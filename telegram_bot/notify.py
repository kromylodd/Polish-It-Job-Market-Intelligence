"""
Telegram notification sender (local pipeline path).

Queries gold mart from the local pipeline DuckDB and sends each subscribed user
the listings that match *their* filters. Uses a per-(listing, chat) idempotency
log (stored locally in the same DuckDB) so runs are safe to repeat, and so a
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
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import requests

from telegram_bot import config_store
from telegram_bot.filters import DEFAULT_USER_CONFIG, filter_listings

logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

TELEGRAM_API_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

# Local pipeline DuckDB — same file the pipeline writes to.
# The bot reads it with read_only=True so it doesn't block the pipeline.
PIPELINE_DB_PATH = Path(
    os.environ.get(
        "PIPELINE_DB_PATH",
        str(Path(__file__).parent.parent / "pipeline.duckdb"),
    )
)

# Idempotency table — stored in the pipeline DuckDB under a metadata schema.
ALERTS_SCHEMA = "meta"
ALERTS_SENT_TABLE = f"{ALERTS_SCHEMA}.telegram_alerts_sent"

# Max listings to send to a single user per run (free-tier default). Paid users
# get a larger cap, published per-user as ``max_listings`` in the shared config
# by the bot (which owns payments.db); we read it here without needing that DB.
MAX_PER_USER = 20


def _get_duckdb_connection(read_only: bool = True):
    """Open a DuckDB connection to the pipeline database."""
    try:
        import duckdb
    except ImportError:
        logger.error("duckdb not installed; cannot read pipeline data")
        return None

    if not PIPELINE_DB_PATH.exists():
        logger.error("Pipeline database not found: %s", PIPELINE_DB_PATH)
        return None

    try:
        return duckdb.connect(str(PIPELINE_DB_PATH), read_only=read_only)
    except Exception as e:
        logger.error("Failed to open pipeline database: %s", e)
        return None


def load_all_user_configs() -> dict[str, dict]:
    """Return {chat_id: config} for every subscriber, merged over defaults.

    Preference order:
      1. A local user_config.json (e.g. when running on the bot host).
      2. Fallback: default filters sent to the admin chat (TELEGRAM_CHAT_ID).
    """

    def _with_defaults(cfg: dict) -> dict:
        merged = copy.deepcopy(DEFAULT_USER_CONFIG)
        merged.update(cfg)
        return merged

    store = config_store.load_local()
    if store:
        return {cid: _with_defaults(cfg) for cid, cfg in store.items() if isinstance(cfg, dict)}

    if TELEGRAM_CHAT_ID:
        logger.info("No user config found; sending default filters to admin chat")
        return {TELEGRAM_CHAT_ID: copy.deepcopy(DEFAULT_USER_CONFIG)}
    return {}


def ensure_alerts_sent_table(con):
    """Create the idempotency log table if it doesn't exist."""
    con.execute(f"CREATE SCHEMA IF NOT EXISTS {ALERTS_SCHEMA}")
    con.execute(f"""
        CREATE TABLE IF NOT EXISTS {ALERTS_SENT_TABLE} (
            listing_id VARCHAR NOT NULL,
            chat_id VARCHAR NOT NULL,
            sent_at TIMESTAMP DEFAULT current_timestamp,
            PRIMARY KEY (listing_id, chat_id)
        )
    """)


def query_recent_listings(con) -> list[dict]:
    """Query the gold mart snapshot of active listings.

    We intentionally do NOT filter by ``posted_date``: justjoin's posted_date is
    the original posting date (often weeks old), so a recency window would return
    almost nothing. Novelty ("what to alert") is defined by the per-(listing, chat)
    idempotency log instead, so each user is alerted about a given listing once.
    """
    try:
        result = con.execute("""
            SELECT listing_id, title, slug, company_name, seniority,
                   employment_type, workplace_type, category,
                   salary_min, salary_max, currency,
                   posted_date, technologies, cities
            FROM gold.mart_market_snapshot
            ORDER BY posted_date DESC
            LIMIT 500
        """).fetchall()

        columns = [
            "listing_id",
            "title",
            "slug",
            "company_name",
            "seniority",
            "employment_type",
            "workplace_type",
            "category",
            "salary_min",
            "salary_max",
            "currency",
            "posted_date",
            "technologies",
            "cities",
        ]
        return [_normalize_row(dict(zip(columns, row))) for row in result]
    except Exception as e:
        logger.error("Failed to query market_snapshot: %s", e)
        return []


def _normalize_row(row: dict) -> dict:
    """Convert array columns (DuckDB lists) to plain Python lists and ensure
    numeric types are floats so downstream formatting doesn't crash."""
    normalized = {}
    for k, v in row.items():
        if isinstance(v, (list, tuple)):
            normalized[k] = list(v) if v else []
        elif hasattr(v, "tolist"):
            normalized[k] = v.tolist()
        else:
            normalized[k] = v
    return normalized


def get_already_sent_pairs(con) -> set[tuple[str, str]]:
    """Return the set of (listing_id, chat_id) that have already been notified."""
    try:
        result = con.execute(f"SELECT listing_id, chat_id FROM {ALERTS_SENT_TABLE}").fetchall()
        return {(row[0], row[1]) for row in result}
    except Exception:
        return set()


def record_sent(con, pairs: list[tuple[str, str]]):
    """Insert (listing_id, chat_id) pairs into the idempotency log."""
    if not pairs:
        return
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    con.executemany(
        f"INSERT OR IGNORE INTO {ALERTS_SENT_TABLE} (listing_id, chat_id, sent_at) "
        f"VALUES (?, ?, ?)",
        [(lid, cid, now) for (lid, cid) in pairs],
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
        try:
            salary_str = (
                f"{int(float(listing['salary_min']))}-{int(float(listing['salary_max']))} "
                f"{listing.get('currency', 'PLN')} ({listing.get('employment_type', '')})"
            )
        except (ValueError, TypeError):
            pass

    slug = listing.get("slug", "")
    link = f"\nhttps://justjoin.it/offers/{quote(str(slug), safe='')}" if slug else ""

    def esc(value: object) -> str:
        return html.escape(str(value))

    pct_line = ""
    if listing.get("match_pct") is not None:
        pct_line = f"🎯 {listing['match_pct']}% skill match\n"

    return (
        f"{pct_line}"
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


def _build_combined_message(listings: list[dict]) -> list[str]:
    """Build combined message(s) from listings, respecting Telegram's 4096 char limit.

    Returns a list of message chunks, each under the limit. The first chunk
    includes a header; subsequent chunks are continuations.
    """
    MAX_MSG_LEN = 4096
    header = f"<b>📋 Daily alert — {len(listings)} new matches</b>\n"
    separator = "\n———\n"

    chunks: list[str] = []
    current = header

    for listing in listings:
        formatted = format_listing(listing)
        # Check if adding this listing would exceed the limit
        addition = separator + formatted if current != header else "\n" + formatted
        if len(current) + len(addition) > MAX_MSG_LEN:
            # Flush current chunk and start a new one
            chunks.append(current)
            current = formatted
        else:
            current += addition

    if current:
        chunks.append(current)

    return chunks


def _cap_for(config: dict) -> int:
    """Per-user listings cap: the bot stamps ``max_listings`` (from the user's
    subscription tier) into the published config; free users fall back to the
    default. Guards against corrupt values."""
    try:
        cap = int(config.get("max_listings", MAX_PER_USER))
        return cap if cap > 0 else MAX_PER_USER
    except (TypeError, ValueError):
        return MAX_PER_USER


def broadcast(con) -> int:
    """Send each user their matching, not-yet-sent listings as combined messages.

    Instead of spamming one message per listing, all matches are batched into as
    few messages as possible (respecting Telegram's 4096-char limit).
    Returns total listings notified about.
    """
    listings = query_recent_listings(con)
    logger.info(f"Fetched {len(listings)} active listings from the mart")

    already_sent = get_already_sent_pairs(con)
    user_configs = load_all_user_configs()
    if not user_configs:
        logger.warning("No recipients configured; nothing to send")
        return 0

    total_sent = 0
    new_pairs: list[tuple[str, str]] = []

    for chat_id, config in user_configs.items():
        matches = filter_listings(listings, config)
        cap = _cap_for(config)
        to_send = [
            listing
            for listing in matches
            if (listing["listing_id"], str(chat_id)) not in already_sent
        ][:cap]

        if not to_send:
            if listings:
                send_message(
                    chat_id,
                    "📭 No new listings matching your filters today.\n"
                    "We'll check again tomorrow! Adjust /filters if you'd like broader results.",
                )
            continue

        # Build combined message(s) and send
        chunks = _build_combined_message(to_send)
        for chunk in chunks:
            send_message(chat_id, chunk)
            time.sleep(0.5)

        # Record all as sent
        pairs = [(listing["listing_id"], str(chat_id)) for listing in to_send]
        new_pairs.extend(pairs)
        total_sent += len(pairs)
        logger.info(f"chat {chat_id}: sent {len(pairs)} listings in {len(chunks)} message(s)")

    # Batch-write all sent records (use a write connection)
    if new_pairs:
        write_con = _get_duckdb_connection(read_only=False)
        if write_con:
            try:
                record_sent(write_con, new_pairs)
                write_con.commit()
            finally:
                write_con.close()

    return total_sent


def run_daily_broadcast() -> int:
    """Send the daily digest — called by the bot's 08:00 Warsaw scheduler.

    Decoupled from pipeline timing, so users get a predictable daily digest
    regardless of when the scrape/pipeline run finished. Reads the gold snapshot
    from the local pipeline DuckDB (instant, no network).
    Returns the number of listings sent.
    """
    if not TELEGRAM_BOT_TOKEN:
        logger.error("Daily broadcast skipped: TELEGRAM_BOT_TOKEN not set")
        return 0

    con = _get_duckdb_connection(read_only=True)
    if con is None:
        logger.error("Daily broadcast skipped: cannot open pipeline database")
        return 0

    try:
        # Ensure idempotency table exists (needs write access for first run)
        write_con = _get_duckdb_connection(read_only=False)
        if write_con:
            try:
                ensure_alerts_sent_table(write_con)
                write_con.commit()
            finally:
                write_con.close()

        return broadcast(con)
    finally:
        con.close()


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if not TELEGRAM_BOT_TOKEN:
        logger.error("Missing TELEGRAM_BOT_TOKEN")
        sys.exit(1)

    if not PIPELINE_DB_PATH.exists():
        logger.error("Pipeline database not found: %s", PIPELINE_DB_PATH)
        logger.error("Run the pipeline first: python -m pipeline.run_pipeline")
        sys.exit(1)

    con = _get_duckdb_connection(read_only=True)
    if con is None:
        sys.exit(1)

    try:
        # Ensure idempotency table exists
        write_con = _get_duckdb_connection(read_only=False)
        if write_con:
            try:
                ensure_alerts_sent_table(write_con)
                write_con.commit()
            finally:
                write_con.close()

        total = broadcast(con)
        logger.info(f"Done — {total} notifications sent")
    finally:
        con.close()


if __name__ == "__main__":
    main()
