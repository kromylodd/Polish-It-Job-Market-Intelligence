# Databricks notebook source
# Telegram alert: query gold for new matching listings, push via Bot API.
#
# May need to run outside Databricks (GitHub Actions) if api.telegram.org
# is not on Free Edition's outbound allowlist. Test connectivity first.

import html
import json
import os
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import requests
from pyspark.sql import SparkSession
from pyspark.sql.functions import col

spark = SparkSession.builder.getOrCreate()

# Config
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
TELEGRAM_API_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

ALERT_FILTERS = {
    "seniorities": ["junior", "mid"],
    "technologies": ["Python", "SQL", "Apache Spark", "dbt", "Apache Airflow"],
    "workplace_types": ["remote", "hybrid"],
}


def check_telegram_connectivity() -> bool:
    try:
        r = requests.get("https://api.telegram.org", timeout=10)
        return r.status_code == 200
    except requests.RequestException:
        print("Cannot reach api.telegram.org from this compute")
        return False


def normalize_row(row) -> dict:
    """Normalize a Spark Row from either the gold mart or the silver fallback
    into a single flat dict, so downstream match/format logic is schema-agnostic.

    - gold mart exposes: technologies, salary_min/max, currency, employment_type
    - silver fallback exposes: all_technologies, salary_variants (array of structs)
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

    # slug builds a URL (quote it); everything else is HTML-escaped since we
    # send with parse_mode=HTML and titles/company names are free text.
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


# Check connectivity
can_reach = check_telegram_connectivity()

# Query new listings.
# The gold mart exposes `posted_date` (a DATE); the silver fallback table
# exposes `date_collected` (an ISO-8601 string). Use the right column/type for each.
yesterday_date = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
yesterday_iso = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()

try:
    df = spark.table("job_market.gold.mart_junior_market_snapshot").filter(
        col("posted_date") >= yesterday_date
    )
except Exception:
    df = (
        spark.table("job_market.silver.listings_with_tech")
        .filter(col("date_collected") >= yesterday_iso)
        .filter(col("seniority").isin(ALERT_FILTERS["seniorities"]))
    )

matching = []
for row in df.collect():
    listing = normalize_row(row)
    if any(t in listing["technologies"] for t in ALERT_FILTERS["technologies"]):
        matching.append(listing)

print(f"Found {len(matching)} matching listings")

# Send
if can_reach and matching:
    send_message(f"<b>Daily alert</b> - {len(matching)} new matches\n")
    sent = 0
    for m in matching[:20]:
        if send_message(format_listing(m)):
            sent += 1
        time.sleep(0.5)  # stay under Telegram's per-chat rate limit
    print(f"Sent {sent}/{len(matching)} notifications")
elif not can_reach:
    print("Telegram unreachable from Databricks; use GitHub Actions fallback")
    dbutils.notebook.exit(json.dumps({"status": "telegram_unreachable", "count": len(matching)}))  # noqa: F821
else:
    print("No matching listings")
