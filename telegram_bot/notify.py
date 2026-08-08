"""
Telegram notification sender (GitHub Actions path).

Queries gold mart via databricks-sql-connector, sends matching listings
to Telegram. Uses the idempotency log (job_market.gold.telegram_alerts_sent)
so both this and the Databricks notebook path can run unconditionally without
producing duplicate alerts.
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

ALERTS_SENT_TABLE = "job_market.gold.telegram_alerts_sent"


def get_sql_connection():
    """Create a Databricks SQL connection."""
    from databricks import sql

    return sql.connect(
        server_hostname=DATABRICKS_HOST.replace("https://", ""),
        http_path=f"/sql/1.0/warehouses/{DATABRICKS_WAREHOUSE_ID}",
        access_token=DATABRICKS_TOKEN,
    )


def ensure_alerts_sent_table(conn):
    """Create the idempotency log table if it doesn't exist."""
    with conn.cursor() as cursor:
        cursor.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {ALERTS_SENT_TABLE} (
                listing_id BIGINT,
                sent_at TIMESTAMP
            )
            USING DELTA
        """
        )


def query_new_listings(conn) -> list[dict]:
    """Query gold mart for new matching listings, excluding already-sent."""
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")

    query = f"""
        SELECT listing_id, title, slug, company_name, seniority,
               employment_type, workplace_type, category,
               salary_min, salary_max, currency,
               posted_date, technologies, cities
        FROM job_market.gold.mart_junior_market_snapshot
        WHERE posted_date >= %(since)s
          AND listing_id NOT IN (
              SELECT listing_id FROM {ALERTS_SENT_TABLE}
          )
        ORDER BY posted_date DESC
        LIMIT 50
    """

    with conn.cursor() as cursor:
        cursor.execute(query, {"since": yesterday})
        columns = [desc[0] for desc in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]


def record_sent_ids(conn, listing_ids: list[int]):
    """Insert successfully-sent listing_ids into the idempotency log."""
    if not listing_ids:
        return
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    # Build a multi-row INSERT.
    values = ", ".join(f"({lid}, '{now}')" for lid in listing_ids)
    with conn.cursor() as cursor:
        cursor.execute(f"INSERT INTO {ALERTS_SENT_TABLE} (listing_id, sent_at) VALUES {values}")


def format_listing(listing: dict) -> str:
    """Format listing as Telegram HTML message."""
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

    if not DATABRICKS_WAREHOUSE_ID:
        logger.error("DATABRICKS_WAREHOUSE_ID is required")
        sys.exit(1)

    conn = get_sql_connection()
    try:
        ensure_alerts_sent_table(conn)
        listings = query_new_listings(conn)
        logger.info(f"Found {len(listings)} new listings (after dedup)")

        if not listings:
            return

        send_message(f"<b>Daily alert</b> — {len(listings)} new matches\n")

        successfully_sent: list[int] = []
        for listing in listings[:20]:
            if send_message(format_listing(listing)):
                successfully_sent.append(listing["listing_id"])
            time.sleep(0.5)

        # Record sent IDs in idempotency log
        record_sent_ids(conn, successfully_sent)
        logger.info(f"Sent {len(successfully_sent)}/{len(listings)} notifications")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
