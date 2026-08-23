"""Scheduled background jobs: the daily digest broadcast and renewal reminders.

Both jobs alert the admin on failure. A background job that quietly dies is a
classic *silent* failure — the digest just stops arriving one morning with
nothing in the foreground to signal it — so surfacing the error to the admin is
part of the observability story, not just logging.
"""

from __future__ import annotations

import asyncio
import datetime
import logging

from telegram.ext import ContextTypes

from telegram_bot import payments
from telegram_bot.handlers.common import ADMIN_CHAT_ID

logger = logging.getLogger(__name__)


async def _alert_admin(context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    """Best-effort admin notification; never raises."""
    if not ADMIN_CHAT_ID:
        return
    try:
        await context.bot.send_message(ADMIN_CHAT_ID, text)
    except Exception:
        logger.exception("Failed to send admin alert")


async def _daily_broadcast_job(context: ContextTypes.DEFAULT_TYPE):
    """Send daily matching listings to all users at 08:00 Warsaw.

    This decouples the broadcast from pipeline completion — users always get
    their digest at a predictable time regardless of scrape/pipeline timing.
    """
    logger.info("Daily broadcast triggered (08:00 Warsaw)")
    try:
        from telegram_bot.notify import run_daily_broadcast

        total = await asyncio.to_thread(run_daily_broadcast)
        logger.info(f"Daily broadcast complete: {total} listings sent")
    except Exception as e:
        logger.error(f"Daily broadcast failed: {e}")
        await _alert_admin(context, f"⚠️ Daily broadcast failed: {e}")


async def _renewal_reminder_job(context: ContextTypes.DEFAULT_TYPE):
    """Nudge users whose subscription is expiring soon or in its grace window."""
    try:
        due = await asyncio.to_thread(payments.due_for_reminder)
    except Exception as e:
        logger.warning("Renewal reminder scan failed: %s", e)
        await _alert_admin(context, f"⚠️ Renewal reminder scan failed: {e}")
        return
    for sub in due:
        tier_name = payments.TIERS.get(sub["tier"], {}).get("name", sub["tier"])
        exp = datetime.datetime.fromtimestamp(sub["expires_at"]).strftime("%Y-%m-%d")
        if sub["status"] == "grace":
            text = (
                f"⏳ Your <b>{tier_name}</b> subscription expired on {exp} and is in a "
                f"{payments.GRACE_DAYS}-day grace period. Renew with /subscribe to keep "
                "premium access without interruption."
            )
        else:
            text = (
                f"🔔 Your <b>{tier_name}</b> subscription expires on {exp}. "
                "Renew with /subscribe to keep your premium features."
            )
        try:
            await context.bot.send_message(int(sub["chat_id"]), text, parse_mode="HTML")
            await asyncio.to_thread(payments.mark_reminded, sub["chat_id"])
        except Exception as e:
            logger.warning("Failed to send renewal reminder to %s: %s", sub["chat_id"], e)
