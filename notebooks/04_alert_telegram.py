# Databricks notebook source
# MAGIC %md
# MAGIC # Telegram Alert
# MAGIC Query gold marts for new listings matching saved filters,
# MAGIC format and send via Telegram Bot API.
# MAGIC
# MAGIC **Note**: This notebook may need to run outside Databricks (in GitHub Actions)
# MAGIC if `api.telegram.org` is not on the Free Edition outbound allowlist.
# MAGIC Test connectivity first before relying on this as a Workflow task.

# COMMAND ----------

import json
import os
from datetime import datetime, timedelta, timezone

import requests
from pyspark.sql import SparkSession
from pyspark.sql.functions import col

spark = SparkSession.builder.getOrCreate()

# COMMAND ----------

# Configuration — these should be set as Databricks secrets or env vars
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
TELEGRAM_API_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

# Alert filters — listings matching these criteria trigger a notification
ALERT_FILTERS = {
    "seniorities": ["junior", "mid"],
    "technologies": ["Python", "SQL", "Apache Spark", "dbt", "Apache Airflow"],
    "workplace_types": ["remote", "hybrid"],
}

# COMMAND ----------

# MAGIC %md
# MAGIC ## Connectivity Check
# MAGIC Verify we can reach api.telegram.org from this compute environment.

# COMMAND ----------

def check_telegram_connectivity() -> bool:
    """Test if Telegram API is reachable from this compute."""
    try:
        response = requests.get(
            "https://api.telegram.org",
            timeout=10,
        )
        return response.status_code == 200
    except requests.RequestException as e:
        print(f"Cannot reach api.telegram.org: {e}")
        print(
            "Telegram notifications should be sent from GitHub Actions instead. "
            "See telegram_bot/notify.py for the external fallback."
        )
        return False


can_reach_telegram = check_telegram_connectivity()

# COMMAND ----------

# MAGIC %md
# MAGIC ## Query New Matching Listings

# COMMAND ----------

# Get listings collected in the last 24 hours
yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()

# Query from gold mart (once dbt models are built)
# For now, query silver as a placeholder
try:
    new_listings_df = (
        spark.table("job_market.gold.mart_junior_market_snapshot")
        .filter(col("date_collected") >= yesterday)
    )
except Exception:
    # Fallback to silver if gold mart doesn't exist yet
    new_listings_df = (
        spark.table("job_market.silver.listings_with_tech")
        .filter(col("date_collected") >= yesterday)
        .filter(col("seniority").isin(ALERT_FILTERS["seniorities"]))
    )

# Filter by matching technologies
matching_listings = []
for row in new_listings_df.collect():
    all_techs = row.all_technologies or []
    if any(tech in all_techs for tech in ALERT_FILTERS["technologies"]):
        matching_listings.append(row)

print(f"Found {len(matching_listings)} new matching listings")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Format & Send Notifications

# COMMAND ----------

def format_listing_message(listing) -> str:
    """Format a single listing into a Telegram-friendly message."""
    cities = ", ".join(listing.cities) if listing.cities else "Remote"
    techs = ", ".join(listing.all_technologies[:8]) if listing.all_technologies else "N/A"

    # Format salary (first variant)
    salary_str = "Not disclosed"
    if listing.salary_variants:
        sv = listing.salary_variants[0]
        salary_str = f"{sv.salary_min}–{sv.salary_max} {sv.currency} ({sv.employment_type})"

    msg = (
        f"🆕 <b>{listing.title}</b>\n"
        f"🏢 {listing.company_name}\n"
        f"📍 {cities} | {listing.workplace_type}\n"
        f"💰 {salary_str}\n"
        f"🛠 {techs}\n"
        f"📅 {listing.posted_date}\n"
    )

    if listing.slug:
        msg += f"🔗 https://justjoin.it/offers/{listing.slug}\n"

    return msg


def send_telegram_message(text: str) -> bool:
    """Send a message via Telegram Bot API."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram credentials not configured — skipping send")
        return False

    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }

    try:
        response = requests.post(TELEGRAM_API_URL, json=payload, timeout=10)
        response.raise_for_status()
        return True
    except requests.RequestException as e:
        print(f"Failed to send Telegram message: {e}")
        return False

# COMMAND ----------

# Send notifications
if can_reach_telegram and matching_listings:
    # Summary header
    header = f"📊 <b>Daily Job Alert</b> — {len(matching_listings)} new matches\n\n"
    send_telegram_message(header)

    # Send individual listings (batch in groups of 5 to avoid rate limits)
    sent = 0
    for listing in matching_listings[:20]:  # Cap at 20 per run
        msg = format_listing_message(listing)
        if send_telegram_message(msg):
            sent += 1

    print(f"Sent {sent}/{len(matching_listings)} notifications")

elif not can_reach_telegram:
    print("Telegram API not reachable from Databricks. Use GitHub Actions fallback.")
    # Write results to a table for the external notifier to pick up
    dbutils.notebook.exit(json.dumps({  # noqa: F821
        "status": "telegram_unreachable",
        "matching_count": len(matching_listings),
    }))

else:
    print("No new matching listings — nothing to send")
