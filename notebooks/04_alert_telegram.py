# Databricks notebook source
# Telegram alert: query gold for new matching listings, push via Bot API.
#
# May need to run outside Databricks (GitHub Actions) if api.telegram.org
# is not on Free Edition's outbound allowlist. Test connectivity first.

import json
import os
from datetime import datetime, timedelta, timezone

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


def format_listing(listing) -> str:
    cities = ", ".join(listing.cities) if listing.cities else "Remote"
    techs = ", ".join(listing.all_technologies[:8]) if listing.all_technologies else "N/A"

    salary_str = "Undisclosed"
    if listing.salary_variants:
        sv = listing.salary_variants[0]
        salary_str = f"{sv.salary_min}-{sv.salary_max} {sv.currency} ({sv.employment_type})"

    link = f"\nhttps://justjoin.it/offers/{listing.slug}" if listing.slug else ""
    return (
        f"<b>{listing.title}</b>\n"
        f"{listing.company_name} | {cities} | {listing.workplace_type}\n"
        f"{salary_str}\n"
        f"{techs}{link}"
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

# Query new listings
yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()

try:
    df = spark.table("job_market.gold.mart_junior_market_snapshot").filter(
        col("date_collected") >= yesterday
    )
except Exception:
    df = spark.table("job_market.silver.listings_with_tech").filter(
        col("date_collected") >= yesterday
    ).filter(col("seniority").isin(ALERT_FILTERS["seniorities"]))

matching = []
for row in df.collect():
    techs = row.all_technologies or []
    if any(t in techs for t in ALERT_FILTERS["technologies"]):
        matching.append(row)

print(f"Found {len(matching)} matching listings")

# Send
if can_reach and matching:
    send_message(f"<b>Daily alert</b> - {len(matching)} new matches\n")
    sent = sum(1 for m in matching[:20] if send_message(format_listing(m)))
    print(f"Sent {sent}/{len(matching)} notifications")
elif not can_reach:
    print("Telegram unreachable from Databricks; use GitHub Actions fallback")
    dbutils.notebook.exit(json.dumps({"status": "telegram_unreachable", "count": len(matching)}))  # noqa: F821
else:
    print("No matching listings")
