"""
Telegram notification sender — external fallback.

This script runs in GitHub Actions (after the Databricks pipeline completes)
if api.telegram.org is not reachable from Databricks Free Edition serverless compute.

Queries the gold mart for new matching listings via databricks-sql-connector,
formats them, and pushes via Telegram Bot API.
"""

import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone

import requests

logger = logging.getLogger(__name__)

# Configuration via environment variables (set in GitHub Actions secrets)
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
DATABRICKS_HOST = os.environ.get("DATABRICKS_HOST", "")
DATABRICKS_TOKEN = os.environ.get("DATABRICKS_TOKEN", "")
DATABRICKS_WAREHOUSE_ID = os.environ.get("DATABRICKS_WAREHOUSE_ID", "")

TELEGRAM_API_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

# Alert filters
ALERT_SENIORITIES = ["junior", "mid"]
ALERT_TECHNOLOGIES = ["Python", "SQL", "Apache Spark", "dbt", "Apache Airflow"]
ALERT_WORKPLACE_TYPES = ["remote", "hybrid"]


def query_new_listings() -> list[dict]:
    """Query gold mart for new listings matching filters via Databricks SQL connector."""
    from databricks import sql

    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")

    query = f"""
        SELECT
            listing_id, title, slug, company_name, seniority,
            employment_type, workplace_type, category,
            salary_min, salary_max, currency,
            posted_date, technologies, cities
        FROM job_market.gold.mart_junior_market_snapshot
        WHERE posted_date >= '{yesterday}'
        ORDER BY posted_date DESC
        LIMIT 50
    """

    with sql.connect(
        server_hostname=DATABRICKS_HOST.replace("https://", ""),
        http_path=f"/sql/1.0/warehouses/{DATABRICKS_WAREHOUSE_ID}",
        access_token=DATABRICKS_TOKEN,
    ) as conn:
        with conn.cursor() as cursor:
            cursor.execute(query)
            columns = [desc[0] for desc in cursor.description]
            rows = cursor.fetchall()
            return [dict(zip(columns, row)) for row in rows]


def format_listing_message(listing: dict) -> str:
    """Format a listing into a Telegram-friendly HTML message."""
    cities = listing.get("cities", "Remote")
    if isinstance(cities, list):
        cities = ", ".join(cities) if cities else "Remote"

    techs = listing.get("technologies", [])
    if isinstance(techs, list):
        techs = ", ".join(techs[:8])
    else:
        techs = str(techs) if techs else "N/A"

    salary_str = "Not disclosed"
    salary_min = listing.get("salary_min")
    salary_max = listing.get("salary_max")
    currency = listing.get("currency", "PLN")
    emp_type = listing.get("employment_type", "")
    if salary_min and salary_max:
        salary_str = f"{int(salary_min)}–{int(salary_max)} {currency} ({emp_type})"

    msg = (
        f"🆕 <b>{listing['title']}</b>\n"
        f"🏢 {listing.get('company_name', 'Unknown')}\n"
        f"📍 {cities} | {listing.get('workplace_type', '')}\n"
        f"💰 {salary_str}\n"
        f"🛠 {techs}\n"
        f"📅 {listing.get('posted_date', '')}\n"
    )

    slug = listing.get("slug")
    if slug:
        msg += f"🔗 https://justjoin.it/offers/{slug}\n"

    return msg


def send_telegram_message(text: str) -> bool:
    """Send a message via Telegram Bot API."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.error("Telegram credentials not configured")
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
        logger.error(f"Failed to send Telegram message: {e}")
        return False


def main():
    """Main entry point — query listings and send notifications."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if not all([TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, DATABRICKS_HOST, DATABRICKS_TOKEN]):
        logger.error("Missing required environment variables")
        sys.exit(1)

    logger.info("Querying new matching listings from gold mart...")
    listings = query_new_listings()
    logger.info(f"Found {len(listings)} new matching listings")

    if not listings:
        logger.info("No new listings — nothing to send")
        return

    # Send summary header
    header = f"📊 <b>Daily Job Alert</b> — {len(listings)} new matches\n\n"
    send_telegram_message(header)

    # Send individual listings (cap at 20)
    sent = 0
    for listing in listings[:20]:
        msg = format_listing_message(listing)
        if send_telegram_message(msg):
            sent += 1

    logger.info(f"Sent {sent}/{len(listings)} notifications via Telegram")


if __name__ == "__main__":
    main()
