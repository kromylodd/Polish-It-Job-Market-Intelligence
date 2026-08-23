"""Interactive Telegram bot for Polish IT Job Market Intelligence.

Universal bot covering all IT roles with adjustable tolerance matching.

This module is a thin assembler: it wires the handler callables (grouped by
domain under ``telegram_bot.handlers``) onto a python-telegram-bot
``Application`` and starts long-polling. The actual command/callback logic lives
in the handler modules; shared state and helpers live in
``telegram_bot.handlers.common``.

Run with: python -m telegram_bot.bot
"""

from __future__ import annotations

import datetime
import logging

from telegram import BotCommandScopeAllPrivateChats, BotCommandScopeChat
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    PreCheckoutQueryHandler,
)
from telegram.ext import filters as tg_filters

from telegram_bot import observability
from telegram_bot.handlers import jobs
from telegram_bot.handlers.analytics_admin import cmd_analytics
from telegram_bot.handlers.common import (
    ADMIN_CHAT_ID,
    ADMIN_COMMANDS,
    BOT_COMMANDS,
    BROADCAST_HOUR,
    BROADCAST_MINUTE,
    RENEWAL_CHECK_INTERVAL_SECONDS,
    TELEGRAM_BOT_TOKEN,
    WARSAW_TZ,
    _get_latest_listings,
    _get_stats,
    _query_local_cache,
    _read_local_latest,
)
from telegram_bot.handlers.core import cmd_feedback, cmd_help, cmd_privacy, cmd_start
from telegram_bot.handlers.filter_cmds import (
    _callback_filters,
    cmd_category,
    cmd_city,
    cmd_employment,
    cmd_filters,
    cmd_myskills,
    cmd_salary,
    cmd_seniority,
    cmd_tech,
    cmd_tolerance,
    cmd_workplace,
)
from telegram_bot.handlers.listings import cmd_latest, cmd_stats
from telegram_bot.handlers.premium_analytics import (
    cmd_company,
    cmd_export,
    cmd_report,
    cmd_skills,
    cmd_trend,
)
from telegram_bot.handlers.subscriptions import (
    _callback_premium,
    _callback_subscribe,
    cmd_givepremium,
    cmd_premium,
    cmd_refund,
    cmd_revokepremium,
    cmd_subscribe,
    precheckout_callback,
    successful_payment_callback,
)
from telegram_bot.handlers.tracker_cmds import (
    _build_tracker_message,
    _callback_tracker,
    _callback_tracker_page,
    _hydrate_tracker_rows,
    cmd_applied,
    cmd_interested,
    cmd_mytracker,
    cmd_rejected,
)

logger = logging.getLogger(__name__)

# Internals that used to live in this module. A couple of tests and any
# out-of-tree tooling still import them from ``telegram_bot.bot``; listing them
# in ``__all__`` keeps that public surface stable after the split (and tells the
# linter these re-exports are intentional).
__all__ = [
    "build_application",
    "main",
    "post_init",
    "_get_latest_listings",
    "_get_stats",
    "_query_local_cache",
    "_read_local_latest",
    "_build_tracker_message",
    "_hydrate_tracker_rows",
]


async def post_init(application: Application):
    """Register the command menu with Telegram and schedule background jobs."""
    # Tell systemd the service finished starting as early as possible — before
    # the network-bound set_my_commands calls below — so a slow Telegram API at
    # boot can't trip the Type=notify start timeout. No-op off systemd.
    observability.notify_ready()

    # Default menu for all users
    await application.bot.set_my_commands(BOT_COMMANDS, scope=BotCommandScopeAllPrivateChats())
    # Extended menu for admin (includes admin-only commands)
    if ADMIN_CHAT_ID:
        await application.bot.set_my_commands(
            ADMIN_COMMANDS, scope=BotCommandScopeChat(chat_id=ADMIN_CHAT_ID)
        )

    logger.info("Bot command menu registered")

    # Background jobs via the JobQueue. The pipeline writes the gold marts to the
    # local DuckDB directly, so there's no serving-cache sync to schedule.
    jq = application.job_queue
    if jq is not None:
        # Fixed daily broadcast at 08:00 Warsaw (decoupled from pipeline).
        jq.run_daily(
            jobs._daily_broadcast_job,
            time=datetime.time(hour=BROADCAST_HOUR, minute=BROADCAST_MINUTE, tzinfo=WARSAW_TZ),
            name="daily_broadcast",
        )
        # Renewal reminders: nudge users whose subscription is expiring / in grace.
        jq.run_repeating(
            jobs._renewal_reminder_job,
            interval=RENEWAL_CHECK_INTERVAL_SECONDS,
            first=300,
        )
    else:
        logger.warning(
            "JobQueue unavailable — the daily broadcast and renewal reminders "
            "won't run. Install python-telegram-bot[job-queue]."
        )


def build_application() -> Application:
    """Build the Application and register every handler.

    Factored out of ``main`` so the full handler wiring can be exercised in a
    test without starting long-polling — this is the smoke test that guards the
    module split (every handler reference below must resolve to a real callable).
    """
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("filters", cmd_filters))
    app.add_handler(CommandHandler("seniority", cmd_seniority))
    app.add_handler(CommandHandler("tech", cmd_tech))
    app.add_handler(CommandHandler("category", cmd_category))
    app.add_handler(CommandHandler("workplace", cmd_workplace))
    app.add_handler(CommandHandler("employment", cmd_employment))
    app.add_handler(CommandHandler("salary", cmd_salary))
    app.add_handler(CommandHandler("city", cmd_city))
    app.add_handler(CommandHandler("tolerance", cmd_tolerance))
    app.add_handler(CommandHandler("latest", cmd_latest))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("analytics", cmd_analytics))
    app.add_handler(CommandHandler("privacy", cmd_privacy))
    app.add_handler(CommandHandler("feedback", cmd_feedback))

    # Premium analytics + personalization
    app.add_handler(CommandHandler("skills", cmd_skills))
    app.add_handler(CommandHandler("trend", cmd_trend))
    app.add_handler(CommandHandler("company", cmd_company))
    app.add_handler(CommandHandler("myskills", cmd_myskills))
    app.add_handler(CommandHandler("export", cmd_export))
    app.add_handler(CommandHandler("report", cmd_report))

    # Application tracker
    app.add_handler(CommandHandler("applied", cmd_applied))
    app.add_handler(CommandHandler("interested", cmd_interested))
    app.add_handler(CommandHandler("rejected", cmd_rejected))
    app.add_handler(CommandHandler("mytracker", cmd_mytracker))

    # Subscriptions (Telegram Stars)
    app.add_handler(CommandHandler("subscribe", cmd_subscribe))
    app.add_handler(CommandHandler("premium", cmd_premium))
    app.add_handler(PreCheckoutQueryHandler(precheckout_callback))
    app.add_handler(MessageHandler(tg_filters.SUCCESSFUL_PAYMENT, successful_payment_callback))

    # Admin
    app.add_handler(CommandHandler("refund", cmd_refund))
    app.add_handler(CommandHandler("givepremium", cmd_givepremium))
    app.add_handler(CommandHandler("revokepremium", cmd_revokepremium))

    # Patterned callback routers MUST be registered before the catch-all filters
    # handler so tracker/subscribe/premium taps aren't swallowed by _callback_filters.
    app.add_handler(CallbackQueryHandler(_callback_tracker, pattern="^trk:"))
    app.add_handler(CallbackQueryHandler(_callback_tracker_page, pattern="^trkpg:"))
    app.add_handler(CallbackQueryHandler(_callback_subscribe, pattern="^subtier:"))
    app.add_handler(CallbackQueryHandler(_callback_premium, pattern="^prem:"))
    app.add_handler(CallbackQueryHandler(_callback_filters))

    # Observability: global error handler (always) + systemd watchdog ping
    # (only under a systemd unit with WatchdogSec set).
    observability.setup(app, ADMIN_CHAT_ID)

    return app


def main():
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    # Suppress httpx request logs — they contain the bot token in the URL
    # (e.g. POST https://api.telegram.org/bot<TOKEN>/getUpdates).
    logging.getLogger("httpx").setLevel(logging.WARNING)

    if not TELEGRAM_BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN not set")
        return

    app = build_application()

    logger.info("Bot starting (long-polling)...")
    # IMPORTANT: do NOT drop pending updates. A successful_payment that arrives
    # while the bot is restarting must still be processed, or the user is charged
    # without being activated. Activation is idempotent (payments.record_and_activate
    # is keyed on charge_id), so replaying a backlog is safe.
    try:
        app.run_polling(drop_pending_updates=False)
    finally:
        # Best-effort: tell systemd we're going down so a clean stop isn't logged
        # as a watchdog failure.
        observability.notify_stopping()


if __name__ == "__main__":
    main()
