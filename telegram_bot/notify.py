"""
Telegram notification sender (GitHub Actions fallback).

Queries gold mart via databricks-sql-connector, sends matching listings
to Telegram. Used when api.telegram.org is unreachable from Databricks.
"""

import html
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import requests

logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
DATABRICKS_HOST = os.environ.get("DATABRICKS_HOST", "")
DATABRICKS_TOKEN = os.environ.get("DATABRICKS_TOKEN", "")
DATABRICKS_WAREHOUSE_ID = os.environ.get("DATABRICKS_WAREHOUSE_ID", "")

TELEGRAM_API_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

ALERT_SENIORITIES = ["junior", "mid"]
ALERT_TECHNOLOGIES = ["Python", "SQL", "Apache Spark", "dbt", "Apache Airflow"]


def query_new_listings() -> list[dict]:
    """Query gold mart for new matching listings."""
    from databricks import sql

    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")

    # Parameterized query (never interpolate values into SQL directly).
    query = """
        SELECT listing_id, title, slug, company_name, seniority,
               employment_type, workplace_type, category,
               salary_min, salary_max, currency,
               posted_date, technologies, cities
        FROM job_market.gold.mart_junior_market_snapshot
        WHERE posted_date >= %(since)s
        ORDER BY posted_date DESC
        LIMIT 50
    """

    with sql.connect(
        server_hostname=DATABRICKS_HOST.replace("https://", ""),
        http_path=f"/sql/1.0/warehouses/{DATABRICKS_WAREHOUSE_ID}",
        access_token=DATABRICKS_TOKEN,
    ) as conn:
        with conn.cursor() as cursor:
            cursor.execute(query, {"since": yesterday})
            columns = [desc[0] for desc in cursor.description]
            return [dict(zip(columns, row)) for row in cursor.fetchall()]


def format_listing(listing: dict) -> str:
    """Format listing as Telegram HTML message.

    All scraped/free-text values are HTML-escaped before being embedded, since
    the message is sent with parse_mode=HTML. Titles/company names can legally
    contain characters like & < > that would otherwise break or inject markup.
    """
    cities = listing.get("cities", "Remote")
    if isinstance(cities, list):
        cities = ", ".join(cities) if cities else "Remote"

    techs = listing.get("technologies", [])
    if isinstance(techs, list):
        techs = ", ".join(techs[:8])
    else:
        techs = str(techs) if techs else "N/A"

    salary_str = "Undisclosed"
    if listing.get("salary_min") and listing.get("salary_max"):
        salary_str = (
            f"{int(listing['salary_min'])}-{int(listing['salary_max'])} "
            f"{listing.get('currency', 'PLN')} ({listing.get('employment_type', '')})"
        )

    slug = listing.get("slug", "")
    # slug is used to build a URL, not embedded as HTML text — quote it defensively.
    link = f"\nhttps://justjoin.it/offers/{quote(str(slug), safe='')}" if slug else ""

    def esc(value: object) -> str:
        return html.escape(str(value))

    return (
        f"<b>{esc(listing.get('title', ''))}</b>\n"
        f"{esc(listing.get('company_name', ''))} | {esc(cities)} | {esc(listing.get('workplace_type', ''))}\n"
        f"{esc(salary_str)}\n"
        f"{esc(techs)}{link}"
    )


def send_message(text: str) -> bool:
    """Send message via Telegram Bot API."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False

    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        resp = requests.post(TELEGRAM_API_URL, json=payload, timeout=10)
        resp.raise_for_status()
        return True
    except requests.RequestException as e:
        logger.error(f"Telegram send failed: {e}")
        return False


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if not all([TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, DATABRICKS_HOST, DATABRICKS_TOKEN]):
        logger.error("Missing required environment variables")
        sys.exit(1)

    listings = query_new_listings()
    logger.info(f"Found {len(listings)} new listings")

    if not listings:
        return

    send_message(f"<b>Daily alert</b> - {len(listings)} new matches\n")

    sent = 0
    for listing in listings[:20]:
        if send_message(format_listing(listing)):
            sent += 1
        # Telegram allows ~30 msg/s overall but throttles bursts per chat.
        # A small delay keeps us well under the limit and avoids 429s.
        time.sleep(0.5)

    logger.info(f"Sent {sent}/{len(listings)} notifications")


if __name__ == "__main__":
    main()
