"""On-demand listing retrieval: /latest and the admin /stats."""

from __future__ import annotations

import asyncio
import logging

from telegram import Update
from telegram.ext import ContextTypes

from telegram_bot import payments, serving, tracker
from telegram_bot.analytics import log_command
from telegram_bot.handlers.common import (
    ADMIN_CHAT_ID,
    _get_latest_listings,
    _get_stats,
    _require_feature,
    _send_trackable_listing,
    load_config,
)

logger = logging.getLogger(__name__)


async def cmd_latest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Premium: on-demand queries hit the data source directly.
    if not await _require_feature(update, payments.FEATURE_LATEST, "/latest on-demand listings"):
        return
    log_command(update.effective_chat.id, "latest")
    await update.message.reply_text("🔍 Fetching latest listings...")

    config = load_config(update.effective_chat.id)

    cap = payments.listing_cap(update.effective_chat.id)
    try:
        # Databricks / file I/O is blocking — keep it off the event loop.
        listings = await asyncio.to_thread(_get_latest_listings, config, cap)
    except Exception as e:
        logger.error(f"/latest failed: {e}")
        await update.message.reply_text(
            "⚠️ Failed to fetch listings. Data source may be unavailable.\n" "Try again later.",
        )
        return

    if not listings:
        await update.message.reply_text(
            "📭 No recent listings match your filters.\n\n"
            "Try:\n"
            "• Increase /tolerance\n"
            "• Broaden /tech or /category\n"
            "• Check /filters — maybe your criteria are too narrow",
        )
        return

    # Personalization: if the user saved a skill set (/myskills), rank by overlap.
    skills = config.get("skills", [])
    if skills:
        listings = serving.rank_listings_by_skills(listings, skills)

    # Hide listings the user already interacted with (in tracker)
    tracked_ids = tracker.tracked_listing_ids(update.effective_chat.id)
    if tracked_ids:
        listings = [li for li in listings if li.get("listing_id") not in tracked_ids]

    if not listings:
        await update.message.reply_text(
            "📭 No new listings — you've already seen all current matches.\n"
            "Check back tomorrow for fresh listings!",
        )
        return

    count = len(listings)
    display_limit = min(cap, count)

    # Send individual listings with tracker buttons (everyone gets buttons now)
    for listing in listings[:display_limit]:
        await _send_trackable_listing(update.message, listing)
        await asyncio.sleep(0.2)

    if count > display_limit:
        await update.message.reply_text(
            f"<i>Showing {display_limit}/{count}. Narrow your filters for more relevant results.</i>",
            parse_mode="HTML",
        )


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show pipeline statistics (admin only)."""
    if update.effective_chat.id != ADMIN_CHAT_ID:
        return
    log_command(update.effective_chat.id, "stats")
    stats = await asyncio.to_thread(_get_stats)
    text = (
        "<b>📊 Pipeline Statistics</b>\n\n"
        f"📁 Raw files scraped: {stats.get('raw_files', 'N/A')}\n"
        f"📨 Alerts sent (total): {stats.get('alerts_sent', 'N/A')}\n"
        f"📅 Last scrape: {stats.get('last_scrape', 'N/A')}\n"
    )
    await update.message.reply_text(text, parse_mode="HTML")
