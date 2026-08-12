"""
Telegram notification sender (local pipeline path).

Queries gold mart from the local pipeline DuckDB and sends each subscribed user
the listings that match *their* filters. Uses a per-(listing, chat) idempotency
log (stored in a dedicated SQLite DB owned by the bot, NOT the pipeline DuckDB)
so runs are safe to repeat, and so a crash mid-run doesn't re-notify users who
were already messaged.

Keeping the idempotency log out of pipeline.duckdb matters for concurrency:
DuckDB allows only a single read-write process, so writing bot state into the
pipeline's analytical DB would collide with a running pipeline. The pipeline DB
is therefore opened strictly read-only here.

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
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import requests

from telegram_bot import config_store, dbutil, payments
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

# Idempotency log — a dedicated SQLite DB owned by the bot (NOT pipeline.duckdb).
ALERTS_DB_PATH = Path(
    os.environ.get(
        "ALERTS_DB_PATH",
        str(Path(__file__).parent / "alerts.db"),
    )
)
_alerts_lock = threading.Lock()

# Max listings to send to a single user per run. Derived from the user's
# subscription tier via payments.listing_cap() — the bot and the broadcast run on
# the same host, so we read payments.db directly instead of stamping a cap into
# the shared config file.
MAX_PER_USER = payments.FREE_MAX_LISTINGS


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


def _init_alerts(conn):
    """Create the SQLite idempotency log table if it doesn't exist."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS telegram_alerts_sent (
            listing_id TEXT NOT NULL,
            chat_id    TEXT NOT NULL,
            sent_at    TEXT NOT NULL,
            PRIMARY KEY (listing_id, chat_id)
        )
        """
    )
    conn.commit()


def get_already_sent_pairs() -> set[tuple[str, str]]:
    """Return the set of (listing_id, chat_id) that have already been notified."""
    try:
        with dbutil.locked_connection(ALERTS_DB_PATH, _alerts_lock) as conn:
            _init_alerts(conn)
            rows = conn.execute("SELECT listing_id, chat_id FROM telegram_alerts_sent").fetchall()
        return {(row[0], row[1]) for row in rows}
    except Exception as e:
        logger.error("Failed to read idempotency log: %s", e)
        return set()


def record_sent(pairs: list[tuple[str, str]]):
    """Insert (listing_id, chat_id) pairs into the SQLite idempotency log."""
    if not pairs:
        return
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    with dbutil.locked_connection(ALERTS_DB_PATH, _alerts_lock) as conn:
        _init_alerts(conn)
        conn.executemany(
            "INSERT OR IGNORE INTO telegram_alerts_sent (listing_id, chat_id, sent_at) "
            "VALUES (?, ?, ?)",
            [(lid, cid, now) for (lid, cid) in pairs],
        )
        conn.commit()


def alerts_sent_total() -> int:
    """Total number of (listing, chat) notifications recorded (for /stats)."""
    try:
        with dbutil.locked_connection(ALERTS_DB_PATH, _alerts_lock) as conn:
            _init_alerts(conn)
            return conn.execute("SELECT COUNT(*) FROM telegram_alerts_sent").fetchone()[0]
    except Exception:
        return 0


def query_recent_listings(con) -> list[dict]:
    """Query the gold mart snapshot of active listings.

    We intentionally do NOT filter by ``posted_date``: justjoin's posted_date is
    the original posting date (often weeks old), so a recency window would return
    almost nothing. Novelty ("what to alert") is defined by the per-(listing, chat)
    idempotency log instead, so each user is alerted about a given listing once.
    """
    try:
        result = con.execute(
            """
            SELECT listing_id, title, slug, company_name, seniority,
                   employment_type, workplace_type, category,
                   salary_min, salary_max, currency,
                   posted_date, technologies, cities
            FROM gold.mart_market_snapshot
            ORDER BY posted_date DESC
            LIMIT 500
        """
        ).fetchall()

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


def send_message(
    chat_id: str, text: str, reply_markup=None, disable_notification: bool = False
) -> bool:
    """Send message via Telegram Bot API to a specific chat."""
    if not TELEGRAM_BOT_TOKEN or not chat_id:
        return False

    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
        "disable_notification": disable_notification,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
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


def _tracker_markup(listing_id: str) -> dict | None:
    """Build inline keyboard markup with tracker buttons for a listing."""
    if not listing_id:
        return None
    return {
        "inline_keyboard": [
            [
                {"text": "✅ Applied", "callback_data": f"trk:applied:{listing_id}"},
                {"text": "👀 Interested", "callback_data": f"trk:interested:{listing_id}"},
                {"text": "❌ Not interested", "callback_data": f"trk:rejected:{listing_id}"},
            ]
        ]
    }


def _cap_for_chat(chat_id) -> int:
    """Per-user listings cap, derived from the user's subscription tier.

    Falls back to the free default for unknown/malformed chat ids or users with
    no active subscription (payments.listing_cap already returns the free cap).
    """
    try:
        return payments.listing_cap(int(chat_id))
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

    already_sent = get_already_sent_pairs()
    user_configs = load_all_user_configs()
    if not user_configs:
        logger.warning("No recipients configured; nothing to send")
        return 0

    total_sent = 0
    new_pairs: list[tuple[str, str]] = []

    for chat_id, config in user_configs.items():
        matches = filter_listings(listings, config)
        cap = _cap_for_chat(chat_id)
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

        # Skill ranking: if the user has saved skills, rank by overlap and add match_pct
        user_skills = config.get("skills") or []
        if user_skills:
            from telegram_bot.serving import rank_listings_by_skills

            to_send = rank_listings_by_skills(to_send, user_skills)

        # Send a header (with notification sound), then each listing individually
        # with tracker buttons (silently). This gives the user one notification
        # but each listing has its own interactive buttons.
        header = f"<b>📋 Daily alert — {len(to_send)} new matches</b>"
        send_message(chat_id, header, disable_notification=False)
        time.sleep(0.3)

        for listing in to_send:
            text = format_listing(listing)
            lid = listing.get("listing_id", "")
            markup = _tracker_markup(lid)
            send_message(chat_id, text, reply_markup=markup, disable_notification=True)
            time.sleep(0.4)

        # Record all as sent
        pairs = [(listing["listing_id"], str(chat_id)) for listing in to_send]
        new_pairs.extend(pairs)
        total_sent += len(pairs)
        logger.info(f"chat {chat_id}: sent {len(pairs)} listings")

    # Batch-write all sent records to the SQLite idempotency log.
    if new_pairs:
        record_sent(new_pairs)

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
        total = broadcast(con)
        logger.info(f"Done — {total} notifications sent")
    finally:
        con.close()


if __name__ == "__main__":
    main()
