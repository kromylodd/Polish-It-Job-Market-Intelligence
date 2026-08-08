# Databricks notebook source
# Telegram alert: query gold for new matching listings, push via Bot API.
#
# Uses an idempotency log (job_market.gold.telegram_alerts_sent) so that
# both this notebook and the GitHub Actions fallback (telegram_bot/notify.py)
# can run unconditionally without producing duplicate alerts.

import html
import os
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import requests
from pyspark.sql import SparkSession
from pyspark.sql.functions import col
from pyspark.sql.types import LongType, StructField, StructType, TimestampType

spark = SparkSession.builder.getOrCreate()

# Config
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
TELEGRAM_API_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

ALERT_FILTERS = {
    "seniorities": ["junior", "mid"],
    "technologies": ["Python", "SQL", "Apache Spark", "dbt", "Apache Airflow"],
}

ALERTS_SENT_TABLE = "job_market.gold.telegram_alerts_sent"

# Default filter config — used when user_config.json doesn't exist.
# In production, the user sets this via the bot's /tolerance, /tech, etc.
DEFAULT_FILTER_CONFIG = {
    "seniorities": ["junior", "mid"],
    "technologies": [],
    "categories": [],
    "workplace_types": [],
    "employment_types": [],
    "salary_min": 0,
    "cities": [],
    "tolerance": 1,
}


def load_filter_config() -> dict:
    """Load user filter config if available, otherwise use defaults.

    On Databricks this file won't exist (it lives on the local dev machine),
    so we fall back to DEFAULT_FILTER_CONFIG.
    """
    import json as _json

    config_path = "/Workspace/polish-it-job-market-intelligence/telegram_bot/user_config.json"
    try:
        with open(config_path) as f:
            return _json.load(f)
    except (FileNotFoundError, OSError):
        return DEFAULT_FILTER_CONFIG.copy()


def match_listing_tolerance(listing: dict, config: dict) -> bool:
    """Check if listing matches using tolerance logic (same as filters.py)."""
    tolerance = config.get("tolerance", 1)
    mismatches = 0

    seniorities = config.get("seniorities", [])
    if seniorities:
        listing_sen = (listing.get("seniority") or "").lower()
        if listing_sen not in seniorities:
            mismatches += 1

    technologies = config.get("technologies", [])
    if technologies:
        listing_techs = set(listing.get("technologies") or [])
        listing_techs_lower = {t.lower() for t in listing_techs}
        wanted_lower = {t.lower() for t in technologies}
        if not listing_techs_lower & wanted_lower:
            mismatches += 1

    categories = config.get("categories", [])
    if categories:
        listing_cat = (listing.get("category") or "").lower()
        if listing_cat not in [c.lower() for c in categories]:
            mismatches += 1

    workplace_types = config.get("workplace_types", [])
    if workplace_types:
        listing_wp = (listing.get("workplace_type") or "").lower()
        if listing_wp not in [w.lower() for w in workplace_types]:
            mismatches += 1

    employment_types = config.get("employment_types", [])
    if employment_types:
        listing_emp = (listing.get("employment_type") or "").lower()
        if listing_emp not in [e.lower() for e in employment_types]:
            mismatches += 1

    salary_min = config.get("salary_min", 0)
    if salary_min and salary_min > 0:
        listing_sal = listing.get("salary_max")
        if listing_sal is not None and listing_sal < salary_min:
            mismatches += 1

    cities = config.get("cities", [])
    if cities:
        listing_cities = listing.get("cities") or []
        listing_cities_lower = {c.lower() for c in listing_cities}
        wanted_cities_lower = {c.lower() for c in cities}
        if not listing_cities_lower & wanted_cities_lower:
            mismatches += 1

    return mismatches <= tolerance


def check_telegram_connectivity() -> bool:
    try:
        r = requests.get("https://api.telegram.org", timeout=10)
        return r.status_code == 200
    except requests.RequestException:
        print("Cannot reach api.telegram.org from this compute")
        return False


def ensure_alerts_sent_table_exists():
    """Create the idempotency log table if it doesn't exist."""
    spark.sql(
        f"""
        CREATE TABLE IF NOT EXISTS {ALERTS_SENT_TABLE} (
            listing_id BIGINT,
            sent_at TIMESTAMP
        )
        USING DELTA
    """
    )


def get_already_sent_ids() -> set[int]:
    """Return listing_ids that have already been notified."""
    return {row.listing_id for row in spark.table(ALERTS_SENT_TABLE).select("listing_id").collect()}


def record_sent_ids(listing_ids: list[int]):
    """Write successfully-sent listing_ids to the idempotency log."""
    if not listing_ids:
        return
    now = datetime.now(timezone.utc)
    schema = StructType(
        [
            StructField("listing_id", LongType(), False),
            StructField("sent_at", TimestampType(), False),
        ]
    )
    rows = [(lid, now) for lid in listing_ids]
    spark.createDataFrame(rows, schema).write.mode("append").saveAsTable(ALERTS_SENT_TABLE)


def normalize_row(row) -> dict:
    """Normalize a Spark Row from either the gold mart or the silver fallback
    into a single flat dict, so downstream match/format logic is schema-agnostic.
    """
    d = row.asDict(recursive=True)

    technologies = d.get("technologies")
    if technologies is None:
        technologies = d.get("all_technologies") or []

    salary_min = d.get("salary_min")
    salary_max = d.get("salary_max")
    currency = d.get("currency")
    employment_type = d.get("employment_type")
    if salary_min is None and d.get("salary_variants"):
        sv = d["salary_variants"][0]
        salary_min = sv.get("salary_min")
        salary_max = sv.get("salary_max")
        currency = sv.get("currency")
        employment_type = sv.get("employment_type")

    return {
        "listing_id": d.get("listing_id"),
        "title": d.get("title", ""),
        "company_name": d.get("company_name", ""),
        "cities": d.get("cities") or [],
        "workplace_type": d.get("workplace_type", ""),
        "slug": d.get("slug", ""),
        "technologies": technologies or [],
        "salary_min": salary_min,
        "salary_max": salary_max,
        "currency": currency or "PLN",
        "employment_type": employment_type or "",
    }


def format_listing(listing: dict) -> str:
    cities = ", ".join(listing["cities"]) if listing["cities"] else "Remote"
    techs = ", ".join(listing["technologies"][:8]) if listing["technologies"] else "N/A"

    salary_str = "Undisclosed"
    if listing["salary_min"] is not None and listing["salary_max"] is not None:
        salary_str = (
            f"{listing['salary_min']}-{listing['salary_max']} "
            f"{listing['currency']} ({listing['employment_type']})"
        )

    slug = listing["slug"]
    link = f"\nhttps://justjoin.it/offers/{quote(str(slug), safe='')}" if slug else ""

    def esc(value: object) -> str:
        return html.escape(str(value))

    return (
        f"<b>{esc(listing['title'])}</b>\n"
        f"{esc(listing['company_name'])} | {esc(cities)} | {esc(listing['workplace_type'])}\n"
        f"{esc(salary_str)}\n"
        f"{esc(techs)}{link}"
    )


def send_message(text: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        r = requests.post(TELEGRAM_API_URL, json=payload, timeout=10)
        r.raise_for_status()
        return True
    except requests.RequestException:
        return False


# --- Main logic ---

# Check connectivity
can_reach = check_telegram_connectivity()
if not can_reach:
    print("Telegram unreachable from Databricks; GitHub Actions fallback will handle it")
    # Exit early — no point querying if we can't send.

if can_reach:
    # Ensure idempotency log exists
    ensure_alerts_sent_table_exists()
    sent_ids = get_already_sent_ids()

    # Query new listings
    yesterday_date = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")

    try:
        df = spark.table("job_market.gold.mart_junior_market_snapshot").filter(
            col("posted_date") >= yesterday_date
        )
    except Exception:
        yesterday_iso = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        df = spark.table("job_market.silver.listings_with_tech").filter(
            col("date_collected") >= yesterday_iso
        )

    # Filter: match criteria + not already sent
    filter_config = load_filter_config()
    matching = []
    for row in df.collect():
        listing = normalize_row(row)
        lid = listing.get("listing_id")
        if lid is None:
            continue
        if lid in sent_ids:
            continue
        if match_listing_tolerance(listing, filter_config):
            matching.append(listing)

    print(
        f"Found {len(matching)} new matching listings "
        f"(tolerance={filter_config.get('tolerance', 1)}, after dedup)"
    )

    # Send
    if matching:
        send_message(f"<b>Daily alert</b> — {len(matching)} new matches\n")
        successfully_sent: list[int] = []
        for m in matching[:20]:
            if send_message(format_listing(m)):
                successfully_sent.append(m["listing_id"])
            time.sleep(0.5)

        # Record sent listings in idempotency log
        record_sent_ids(successfully_sent)
        print(f"Sent {len(successfully_sent)}/{len(matching)} notifications")
    else:
        print("No new matching listings to send")
