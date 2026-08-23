"""Smoke test guarding the bot.py -> handlers/* module split.

``build_application`` registers every handler by importing its callable from the
domain module. If any import path or symbol broke during the split, building the
Application raises here — so this exercises the full wiring without needing a
network connection or a real bot token.
"""

import os

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test")

from telegram_bot import bot  # noqa: E402


def test_build_application_registers_all_handlers():
    app = bot.build_application()

    # Every command/callback handler lands in the default group (0).
    handlers = app.handlers[0]
    # 33 command/message handlers + 5 callback routers at time of writing;
    # keep the assertion loose so adding a handler doesn't break the test.
    assert len(handlers) >= 30

    # Observability registers a global error handler.
    assert app.error_handlers, "expected an error handler to be registered"


def test_reexports_available_for_compat():
    # Internals that used to live in bot.py must remain importable from it.
    for name in (
        "_query_local_cache",
        "_get_latest_listings",
        "_build_tracker_message",
        "_hydrate_tracker_rows",
    ):
        assert hasattr(bot, name), f"bot.{name} missing after split"
