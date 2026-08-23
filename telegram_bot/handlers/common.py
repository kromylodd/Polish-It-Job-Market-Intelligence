"""Shared state and helpers for all bot handler modules.

This module holds the pieces that multiple handler groups need and must agree
on: bot-wide constants, the per-user config load/save cycle (guarded by a
single process lock), premium feature gating, the recently-shown-listing cache
that inline tracker buttons rely on, and the read paths into the local gold
mart. Everything else imports from here; ``common`` imports no other handler
module, which keeps the package free of import cycles.
"""

from __future__ import annotations

import copy
import html
import json
import logging
import os
import threading
import zoneinfo
from collections import OrderedDict
from pathlib import Path

from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)

from telegram_bot import config_store, payments
from telegram_bot.filters import DEFAULT_USER_CONFIG, filter_listings

logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")


def _parse_admin_chat_id() -> int:
    """Parse the admin chat id from env, tolerating malformed values."""
    raw = os.environ.get("TELEGRAM_CHAT_ID", "0")
    try:
        return int(raw)
    except (TypeError, ValueError):
        logger.warning("TELEGRAM_CHAT_ID is not an integer (%r); admin commands disabled", raw)
        return 0


ADMIN_CHAT_ID = _parse_admin_chat_id()

# --- Daily broadcast timing (08:00 Warsaw, configurable via env) ---
WARSAW_TZ = zoneinfo.ZoneInfo("Europe/Warsaw")
BROADCAST_HOUR = int(os.environ.get("BROADCAST_HOUR", "8"))
BROADCAST_MINUTE = int(os.environ.get("BROADCAST_MINUTE", "0"))

# How often to check for subscriptions needing a renewal nudge.
RENEWAL_CHECK_INTERVAL_SECONDS = int(
    os.environ.get("RENEWAL_CHECK_INTERVAL_SECONDS", str(6 * 3600))
)

# Serializes read-modify-write cycles on the shared config file within this process.
_config_lock = threading.Lock()

BOT_COMMANDS = [
    BotCommand("start", "Welcome + overview"),
    BotCommand("filters", "View & edit your filters"),
    BotCommand("tolerance", "Set mismatch tolerance"),
    BotCommand("tech", "Set your skills (filter + rank)"),
    BotCommand("latest", "Recent matching listings"),
    BotCommand("premium", "📊 Analytics & tools menu"),
    BotCommand("feedback", "Send feedback to the maker"),
    BotCommand("privacy", "Privacy & data info"),
    BotCommand("help", "Show all commands"),
]

# Admin-only commands shown only in the admin's chat menu
ADMIN_COMMANDS = BOT_COMMANDS + [
    BotCommand("stats", "📊 Pipeline statistics"),
    BotCommand("analytics", "📊 Usage analytics (reset)"),
    BotCommand("givepremium", "🎁 Grant premium to user"),
    BotCommand("revokepremium", "🚫 Revoke premium from user"),
    BotCommand("refund", "↩️ Refund a Stars charge"),
]


def load_config(chat_id: int) -> dict:
    """Load a single user's config, merged over defaults. Never mutates shared state."""
    with _config_lock:
        all_configs = config_store.load_local()
    merged = copy.deepcopy(DEFAULT_USER_CONFIG)
    user_cfg = all_configs.get(str(chat_id))
    if isinstance(user_cfg, dict):
        merged.update(user_cfg)
    return merged


def save_config(chat_id: int, config: dict):
    """Persist a single user's config atomically."""
    with _config_lock:
        all_configs = config_store.load_local()
        all_configs[str(chat_id)] = config
        config_store.save_local(all_configs)


# --- Premium feature gating ---

# Temporary flag: all former "premium" features are free for everyone while
# the paid tier is paused. Flip back to False to re-enable the paywall — the
# payment/subscription machinery underneath is left fully intact.
PREMIUM_FREE = True


def is_paid_user(chat_id: int) -> bool:
    """Whether a user has any active paid subscription (admin always counts)."""
    if PREMIUM_FREE:
        return True
    return chat_id == ADMIN_CHAT_ID or payments.is_subscribed(chat_id)


def has_feature(chat_id: int, feature: str) -> bool:
    """Whether a user can use a premium feature (admin bypasses the paywall)."""
    if PREMIUM_FREE:
        return True
    return chat_id == ADMIN_CHAT_ID or payments.has_feature(chat_id, feature)


def _upsell_text(feature_label: str) -> str:
    """Friendly paywall message pointing users to /subscribe."""
    return (
        f"🔒 <b>{feature_label}</b> is a premium feature.\n\n"
        "Unlock it with a subscription — see /subscribe for tiers and prices.\n"
        "<i>Filters and daily alerts stay free for everyone.</i>"
    )


async def _require_feature(update: Update, feature: str, feature_label: str) -> bool:
    """Gate a handler behind a feature. Sends an upsell + returns False if blocked."""
    if has_feature(update.effective_chat.id, feature):
        return True
    await update.message.reply_text(_upsell_text(feature_label), parse_mode="HTML")
    return False


async def _require_feature_chat(context, chat_id: int, feature: str, feature_label: str) -> bool:
    """Feature gate for flows that only have a chat_id (e.g. inline-menu callbacks)."""
    if has_feature(chat_id, feature):
        return True
    await context.bot.send_message(chat_id, _upsell_text(feature_label), parse_mode="HTML")
    return False


def _esc(value: object) -> str:
    return html.escape(str(value))


def _premium_back_kb() -> InlineKeyboardMarkup:
    """Inline keyboard shown after premium command responses for easy navigation."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("📊 Analytics Menu", callback_data="prem:home"),
                InlineKeyboardButton("🎛️ Filters", callback_data="back_filters"),
            ]
        ]
    )


# --- Recently-shown-listing cache (for inline tracker buttons) ---

# Small in-memory LRU of recently shown listings so tracker inline buttons can
# recover a listing's title/company/url from just its id (kept out of callback
# data, which is limited to 64 bytes). Lost on restart — harmless, since the
# tracker row already persists the metadata captured at button-press time.
# Ordered by recency of use (get + set both refresh) so a heavy user can't evict
# another user's just-shown listing before they tap its button.
_recent_listings: "OrderedDict[str, dict]" = OrderedDict()
_RECENT_MAX = 2000


def _recall_listing(lid: str) -> dict:
    """Look up cached listing metadata by id, refreshing its recency. {} if absent."""
    meta = _recent_listings.get(lid)
    if meta is None:
        return {}
    _recent_listings.move_to_end(lid)
    return meta


def _remember_listing(listing: dict) -> str:
    """Cache a listing's display metadata keyed by its id. Returns the id."""
    lid = str(listing.get("listing_id", "") or "")
    if not lid:
        return lid
    slug = listing.get("slug", "")
    url = f"https://justjoin.it/offers/{slug}" if slug else ""
    _recent_listings[lid] = {
        "title": listing.get("title"),
        "company": listing.get("company_name"),
        "url": url,
    }
    _recent_listings.move_to_end(lid)
    while len(_recent_listings) > _RECENT_MAX:
        _recent_listings.popitem(last=False)  # evict least-recently-used
    return lid


async def _send_trackable_listing(message, listing: dict):
    """Send one listing with Applied/Interested/Rejected inline buttons."""
    from telegram_bot.notify import format_listing

    lid = _remember_listing(listing)
    text = format_listing(listing)

    markup = None
    if lid:
        markup = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("✅ Applied", callback_data=f"trk:applied:{lid}"),
                    InlineKeyboardButton("👀 Interested", callback_data=f"trk:interested:{lid}"),
                    InlineKeyboardButton("❌ Rejected", callback_data=f"trk:rejected:{lid}"),
                ]
            ]
        )
    await message.reply_text(
        text, parse_mode="HTML", disable_web_page_preview=True, reply_markup=markup
    )


# --- Data helpers (read paths into the local gold mart) ---


def _get_latest_listings(config: dict, limit: int = payments.FREE_MAX_LISTINGS) -> list[dict]:
    """Return filtered recent listings from the local gold mart, with a JSON fallback.

    Primary source is the local DuckDB serving cache (the pipeline's gold mart).
    If that isn't ready (no pipeline run yet, or DuckDB unavailable), fall back to
    the most recent raw scrape file on disk so the bot still returns something.
    """
    try:
        rows = _query_local_cache()
        if rows:
            return filter_listings(rows, config)[:limit]
    except Exception as e:
        logger.warning("Local DuckDB cache read failed, falling back to JSON: %s", e)

    return _read_local_latest(config, limit)


def _query_local_cache() -> list[dict] | None:
    """Query the local DuckDB serving cache for gold market-snapshot rows.

    Uses fetchall()/description (not fetchdf) so we don't require pandas at
    runtime — the bot's dependencies are intentionally lean.
    """
    from telegram_bot import serving

    if not serving.is_ready():
        return None
    import duckdb

    con = duckdb.connect(str(serving.SERVING_DB_PATH), read_only=True)
    try:
        cur = con.execute(
            "SELECT * FROM gold.mart_market_snapshot ORDER BY posted_date DESC LIMIT 500"
        )
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    finally:
        con.close()

    if not rows:
        return None

    # Normalize DuckDB arrays / numpy scalars to plain Python.
    for row in rows:
        for k, v in row.items():
            if isinstance(v, (list, tuple)):
                row[k] = list(v)
            elif hasattr(v, "tolist"):
                row[k] = v.tolist()
            elif hasattr(v, "item"):
                row[k] = v.item()
    return rows


def _read_local_latest(config: dict, limit: int = payments.FREE_MAX_LISTINGS) -> list[dict]:
    """Read most recent local data file and filter."""
    data_dir = Path(__file__).parent.parent.parent / "data"
    if not data_dir.exists():
        return []

    files = sorted(data_dir.glob("raw_listings_*.json"), reverse=True)
    if not files:
        return []

    with open(files[0]) as f:
        data = json.load(f)

    listings = data.get("listings", [])
    return filter_listings(listings[:200], config)[:limit]


def _get_stats() -> dict:
    stats = {}

    data_dir = Path(__file__).parent.parent.parent / "data"
    if data_dir.exists():
        files = list(data_dir.glob("raw_listings_*.json"))
        stats["raw_files"] = len(files)
        if files:
            latest = sorted(files, reverse=True)[0]
            stem = latest.stem
            parts = stem.replace("raw_listings_", "")
            stats["last_scrape"] = parts.replace("_", " @ ")
    else:
        stats["raw_files"] = 0
        stats["last_scrape"] = "No data yet"

    try:
        from telegram_bot import notify

        stats["alerts_sent"] = notify.alerts_sent_total()
    except Exception:
        stats["alerts_sent"] = "N/A"

    return stats
