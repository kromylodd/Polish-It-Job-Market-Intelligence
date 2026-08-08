"""
Telegram notification sender (GitHub Actions path).

Queries gold mart via databricks-sql-connector and sends each subscribed user
the listings that match *their* filters. Uses a per-(listing, chat) idempotency
log (job_market.gold.telegram_alerts_sent) so this and the Databricks notebook
path can both run unconditionally without producing duplicate alerts, and so a
crash mid-run doesn't re-notify users who were already messaged.

The recipient list + per-user filters come from telegram_bot/user_config.json
({chat_id: config}). If that file isn't available (e.g. a fresh CI checkout),
we fall back to sending the default filter set to TELEGRAM_CHAT_ID, preserving
the original single-recipient behavior.
"""

import copy
import html
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import requests

from telegram_bot import config_store
from telegram_bot.filters import DEFAULT_USER_CONFIG, filter_listings

logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
DATABRICKS_HOST = os.environ.get("DATABRICKS_HOST", "")
DATABRICKS_TOKEN = os.environ.get("DATABRICKS_TOKEN", "")
DATABRICKS_WAREHOUSE_ID = os.environ.get("DATABRICKS_WAREHOUSE_ID", "")

TELEGRAM_API_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

ALERTS_SENT_TABLE = "job_market.gold.telegram_alerts_sent"

# How far back to look for listings. Wider than the daily cadence so that a late
# or failed pipeline run doesn't cause listings to age out before they're ever
# sent — the per-(listing, chat) idempotency log prevents duplicates.
LOOKBACK_DAYS = 3

# Max listings to send to a single user per run.
MAX_PER_USER = 20


def load_all_user_configs() -> dict[str, dict]:
    """Return {chat_id: config} for every subscriber, merged over defaults.

    Preference order:
      1. The shared Databricks Volume (published by the bot) — the real source.
      2. A local user_config.json (e.g. when running on the bot host).
      3. Fallback: default filters sent to the admin chat (TELEGRAM_CHAT_ID).
    """

    def _with_defaults(cfg: dict) -> dict:
        merged = copy.deepcopy(DEFAULT_USER_CONFIG)
        merged.update(cfg)
        return merged

    store = config_store.download_from_volume() or config_store.load_local()
    if store:
        return {cid: _with_defaults(cfg) for cid, cfg in store.items() if isinstance(cfg, dict)}

    if TELEGRAM_CHAT_ID:
        logger.info("No user config found; sending default filters to admin chat")
        return {TELEGRAM_CHAT_ID: copy.deepcopy(DEFAULT_USER_CONFIG)}
    return {}


def get_sql_connection():
    """Create a Databricks SQL connection."""
    from databricks import sql

    return sql.connect(
        server_hostname=DATABRICKS_HOST.replace("https://", ""),
        http_path=f"/sql/1.0/warehouses/{DATABRICKS_WAREHOUSE_ID}",
        access_token=DATABRICKS_TOKEN,
    )


def ensure_alerts_sent_table(conn):
    """Create the idempotency log table, migrating older single-column layouts."""
    with conn.cursor() as cursor:
        cursor.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {ALERTS_SENT_TABLE} (
                listing_id BIGINT,
                chat_id STRING,
                sent_at TIMESTAMP
            )
            USING DELTA
        """
        )
        # Older tables were (listing_id, sent_at); add chat_id if it's missing.
        try:
            cursor.execute(f"ALTER TABLE {ALERTS_SENT_TABLE} ADD COLUMNS (chat_id STRING)")
        except Exception:
            pass  # Column already exists — expected on the happy path.


def query_recent_listings(conn) -> list[dict]:
    """Query gold mart for recently-posted listings (dedup is applied per-user)."""
    since = (datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")

    query = """
        SELECT listing_id, title, slug, company_name, seniority,
               employment_type, workplace_type, category,
               salary_min, salary_max, currency,
               posted_date, technologies, cities
        FROM job_market.gold.mart_junior_market_snapshot
        WHERE posted_date >= %(since)s
        ORDER BY posted_date DESC
        LIMIT 500
    """

    with conn.cursor() as cursor:
        cursor.execute(query, {"since": since})
        columns = [desc[0] for desc in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]


def get_already_sent_pairs(conn) -> set[tuple[int, str]]:
    """Return the set of (listing_id, chat_id) that have already been notified."""
    with conn.cursor() as cursor:
        cursor.execute(f"SELECT listing_id, chat_id FROM {ALERTS_SENT_TABLE}")
        return {(row[0], row[1]) for row in cursor.fetchall()}


def record_sent(conn, pairs: list[tuple[int, str]]):
    """Insert (listing_id, chat_id) pairs into the idempotency log (parameterized)."""
    if not pairs:
        return
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    with conn.cursor() as cursor:
        cursor.executemany(
            f"INSERT INTO {ALERTS_SENT_TABLE} (listing_id, chat_id, sent_at) "
            f"VALUES (%(lid)s, %(cid)s, %(ts)s)",
            [{"lid": lid, "cid": cid, "ts": now} for (lid, cid) in pairs],
        )


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
        f"{esc(listing.get('company_name', ''))} | {esc(cities)} | "
        f"{esc(listing.get('workplace_type', ''))}\n"
        f"{esc(salary_str)}\n"
        f"{esc(techs)}{link}"
    )


def send_message(chat_id: str, text: str) -> bool:
    """Send message via Telegram Bot API to a specific chat."""
    if not TELEGRAM_BOT_TOKEN or not chat_id:
        return False

    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        resp = requests.post(TELEGRAM_API_URL, json=payload, timeout=10)
        resp.raise_for_status()
        return True
    except requests.RequestException as e:
        logger.error(f"Telegram send failed (chat {chat_id}): {e}")
        return False


def broadcast(conn) -> int:
    """Send each user their matching, not-yet-sent listings. Returns total sent."""
    listings = query_recent_listings(conn)
    logger.info(f"Fetched {len(listings)} recent listings (last {LOOKBACK_DAYS}d)")

    already_sent = get_already_sent_pairs(conn)
    user_configs = load_all_user_configs()
    if not user_configs:
        logger.warning("No recipients configured; nothing to send")
        return 0

    total_sent = 0
    for chat_id, config in user_configs.items():
        matches = filter_listings(listings, config)
        to_send = [
            listing
            for listing in matches
            if (listing["listing_id"], str(chat_id)) not in already_sent
        ][:MAX_PER_USER]

        if not to_send:
            continue

        send_message(chat_id, f"<b>Daily alert</b> — {len(to_send)} new matches\n")

        # Record per message so a crash mid-loop can't cause a re-send next run.
        sent_pairs: list[tuple[int, str]] = []
        for listing in to_send:
            if send_message(chat_id, format_listing(listing)):
                sent_pairs.append((listing["listing_id"], str(chat_id)))
            time.sleep(0.5)

        record_sent(conn, sent_pairs)
        total_sent += len(sent_pairs)
        logger.info(f"chat {chat_id}: sent {len(sent_pairs)}/{len(to_send)}")

    return total_sent


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if not all([TELEGRAM_BOT_TOKEN, DATABRICKS_HOST, DATABRICKS_TOKEN]):
        logger.error("Missing required environment variables")
        sys.exit(1)

    if not DATABRICKS_WAREHOUSE_ID:
        logger.error("DATABRICKS_WAREHOUSE_ID is required")
        sys.exit(1)

    conn = get_sql_connection()
    try:
        ensure_alerts_sent_table(conn)
        total = broadcast(conn)
        logger.info(f"Done — {total} notifications sent")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
