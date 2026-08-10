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
from pyspark.sql.types import StringType, StructField, StructType, TimestampType

spark = SparkSession.builder.getOrCreate()

# Config
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
TELEGRAM_API_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

ALERTS_SENT_TABLE = "job_market.gold.telegram_alerts_sent"

# Default filter config — used when user_config.json doesn't exist.
# In production, the user sets this via the bot's /tolerance, /tech, etc.
# Mirrors telegram_bot.filters.DEFAULT_USER_CONFIG (all seniorities enabled) so
# the fallback isn't narrower than the bot's own default now that the alert
# source is the all-seniorities mart_market_snapshot.
DEFAULT_FILTER_CONFIG = {
    "seniorities": ["intern", "junior", "mid", "senior", "lead", "manager", "c_level"],
    "technologies": [],
    "categories": [],
    "workplace_types": [],
    "employment_types": [],
    "salary_min": 0,
    "cities": [],
    "tolerance": 1,
}


def _import_shared_matcher():
    """Prefer the canonical matcher from telegram_bot.filters so this notebook
    can never drift from the bot / GitHub Actions path.

    The bundle deploys the whole repo under .../<target>/files, so adding that
    root to sys.path lets us import the shared module. If the import fails for
    any reason we fall back to the inline mirror below.
    """
    import sys

    target = os.environ.get("DATABRICKS_BUNDLE_TARGET", "prod")
    try:
        user = spark.sql("SELECT current_user()").collect()[0][0]
        root = f"/Workspace/Users/{user}/.bundle/polish-it-job-market-intelligence/{target}/files"
        if root not in sys.path:
            sys.path.insert(0, root)
        from telegram_bot.filters import match_listing

        print("Using shared matcher from telegram_bot.filters")
        return lambda listing, config: match_listing(listing, config)[0]
    except Exception as e:  # pragma: no cover - depends on workspace layout
        print(f"Shared matcher unavailable ({e}); using inline fallback")
        return _match_listing_fallback


def _match_listing_fallback(listing: dict, config: dict) -> bool:
    """Inline mirror of telegram_bot.filters.match_listing.

    MUST stay in sync with filters.py. Operates on the flat dict produced by
    normalize_row (gold-schema fields), so it is behaviorally equivalent to the
    shared implementation for the fields present here.
    """
    tolerance = config.get("tolerance", 1)
    mismatches = 0

    seniorities = config.get("seniorities", [])
    if seniorities:
        listing_sen = (listing.get("seniority") or "").lower()
        if listing_sen not in [s.lower() for s in seniorities]:
            mismatches += 1

    technologies = config.get("technologies", [])
    if technologies:
        listing_techs = {t.lower() for t in (listing.get("technologies") or [])}
        wanted = {t.lower() for t in technologies}
        if not listing_techs & wanted:
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
        if listing_emp and listing_emp not in [e.lower() for e in employment_types]:
            mismatches += 1

    salary_min = config.get("salary_min", 0)
    if salary_min and salary_min > 0:
        listing_sal = listing.get("salary_max")
        # Undisclosed salary -> benefit of the doubt (no mismatch), matching filters.py.
        if listing_sal is not None and listing_sal < salary_min:
            mismatches += 1

    cities = config.get("cities", [])
    if cities:
        listing_cities = {c.lower() for c in (listing.get("cities") or [])}
        wanted_cities = {c.lower() for c in cities}
        if not listing_cities & wanted_cities:
            mismatches += 1

    return mismatches <= tolerance


match_listing_tolerance = _import_shared_matcher()


def load_user_configs() -> dict:
    """Load the {chat_id: config} store published by the bot to the Volume.

    Volumes are FUSE-mounted at /Volumes on Databricks compute, so we can read
    the file directly. Falls back to default filters for the admin chat if the
    file is missing/unreadable (e.g. before the bot has ever published).
    """
    import json as _json

    path = os.environ.get(
        "USER_CONFIG_VOLUME_PATH",
        "/Volumes/job_market/bronze/raw_listings/_config/user_config.json",
    )
    try:
        with open(path) as f:
            data = _json.load(f)
        # Ignore the legacy flat shape; only accept a real per-user store.
        if (
            isinstance(data, dict)
            and data
            and "tolerance" not in data
            and "seniorities" not in data
        ):
            return {
                cid: {**DEFAULT_FILTER_CONFIG, **cfg}
                for cid, cfg in data.items()
                if isinstance(cfg, dict)
            }
    except (FileNotFoundError, OSError, ValueError):
        pass

    if TELEGRAM_CHAT_ID:
        return {TELEGRAM_CHAT_ID: DEFAULT_FILTER_CONFIG.copy()}
    return {}


def check_telegram_connectivity() -> bool:
    try:
        r = requests.get("https://api.telegram.org", timeout=10)
        return r.status_code == 200
    except requests.RequestException:
        print("Cannot reach api.telegram.org from this compute")
        return False


def ensure_alerts_sent_table_exists():
    """Create the idempotency log table if it doesn't exist, migrating old layout."""
    spark.sql(
        f"""
        CREATE TABLE IF NOT EXISTS {ALERTS_SENT_TABLE} (
            listing_id STRING,
            chat_id STRING,
            sent_at TIMESTAMP
        )
        USING DELTA
    """
    )
    # Older tables were (listing_id, sent_at); add chat_id if missing.
    try:
        spark.sql(f"ALTER TABLE {ALERTS_SENT_TABLE} ADD COLUMNS (chat_id STRING)")
    except Exception:
        pass


def get_already_sent_pairs() -> set:
    """Return (listing_id, chat_id) pairs that have already been notified."""
    rows = spark.table(ALERTS_SENT_TABLE).select("listing_id", "chat_id").collect()
    return {(row.listing_id, row.chat_id) for row in rows}


def record_sent_pairs(pairs: list):
    """Write successfully-sent (listing_id, chat_id) pairs to the idempotency log."""
    if not pairs:
        return
    now = datetime.now(timezone.utc)
    schema = StructType(
        [
            StructField("listing_id", StringType(), False),
            StructField("chat_id", StringType(), True),
            StructField("sent_at", TimestampType(), False),
        ]
    )
    rows = [(lid, cid, now) for (lid, cid) in pairs]
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


def send_message(chat_id: str, text: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not chat_id:
        return False
    payload = {
        "chat_id": chat_id,
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
    sent_pairs = get_already_sent_pairs()

    # Query new listings
    yesterday_date = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")

    try:
        df = spark.table("job_market.gold.mart_market_snapshot").filter(
            col("posted_date") >= yesterday_date
        )
    except Exception:
        yesterday_iso = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        df = spark.table("job_market.silver.listings_with_tech").filter(
            col("date_collected") >= yesterday_iso
        )

    normalized = [normalize_row(row) for row in df.collect()]
    normalized = [n for n in normalized if n.get("listing_id") is not None]

    # Recipients + their filters come from the shared config store on the Volume
    # (published by the bot). Falls back to the admin chat with default filters.
    recipients = load_user_configs()

    MAX_PER_USER = 20
    MAX_MSG_LEN = 4096

    def build_combined_message(listings_batch: list) -> list:
        """Combine listings into as few messages as possible (max 4096 chars each)."""
        header = f"<b>📋 Daily alert — {len(listings_batch)} new matches</b>\n"
        separator = "\n———\n"
        chunks = []
        current = header

        for listing in listings_batch:
            formatted = format_listing(listing)
            addition = separator + formatted if current != header else "\n" + formatted
            if len(current) + len(addition) > MAX_MSG_LEN:
                chunks.append(current)
                current = formatted
            else:
                current += addition

        if current:
            chunks.append(current)
        return chunks

    total_sent = 0
    for chat_id, config in recipients.items():
        matching = [
            listing
            for listing in normalized
            if (listing["listing_id"], str(chat_id)) not in sent_pairs
            and match_listing_tolerance(listing, config)
        ]

        print(
            f"chat {chat_id}: {len(matching)} new matching listings "
            f"(tolerance={config.get('tolerance', 1)}, after dedup)"
        )

        if not matching:
            continue

        # Paid users get a larger batch: the bot stamps max_listings (from their
        # subscription tier) into the shared config; free users use the default.
        try:
            cap = int(config.get("max_listings", MAX_PER_USER))
            if cap <= 0:
                cap = MAX_PER_USER
        except (TypeError, ValueError):
            cap = MAX_PER_USER
        to_send = matching[:cap]
        chunks = build_combined_message(to_send)
        for chunk in chunks:
            send_message(chat_id, chunk)
            time.sleep(0.5)

        # Record after each recipient so a mid-run failure can't re-notify them.
        newly_sent = [(m["listing_id"], str(chat_id)) for m in to_send]
        record_sent_pairs(newly_sent)
        total_sent += len(newly_sent)

    print(f"Sent {total_sent} notifications total")
