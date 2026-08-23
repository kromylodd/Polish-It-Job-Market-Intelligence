"""Tests for the global error handler's alerting policy.

Regression guard for the alert-spam bug: transient network errors from the
long-polling fetch loop (getUpdates) arrive at the error handler with
``update=None`` and must NOT page the admin, since PTB retries them
automatically. Errors raised while handling an actual update still alert.
"""

import asyncio
from types import SimpleNamespace

from telegram.error import BadRequest, NetworkError, TimedOut

from telegram_bot import observability

ADMIN = 12345


class _FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, parse_mode=None):
        self.sent.append((chat_id, text))


def _run(handler, update, err):
    bot = _FakeBot()
    ctx = SimpleNamespace(error=err, bot=bot)
    asyncio.run(handler(update, ctx))
    return bot.sent


def test_transient_polling_network_error_does_not_alert():
    handler = observability.make_error_handler(ADMIN)
    # Mirrors Application.run_polling: poll-loop errors dispatch with update=None.
    sent = _run(handler, None, NetworkError("httpx.ReadError: "))
    assert sent == [], "transient polling network error should not page admin"


def test_transient_polling_timeout_does_not_alert():
    handler = observability.make_error_handler(ADMIN)
    sent = _run(handler, None, TimedOut())
    assert sent == [], "transient polling timeout should not page admin"


def test_network_error_during_update_handling_alerts():
    handler = observability.make_error_handler(ADMIN)
    fake_update = object()  # a real update was being processed
    sent = _run(handler, fake_update, NetworkError("send failed"))
    assert len(sent) == 1
    assert sent[0][0] == ADMIN


def test_non_network_polling_error_still_alerts():
    handler = observability.make_error_handler(ADMIN)
    # A non-network TelegramError from the poll loop is unusual and worth seeing.
    sent = _run(handler, None, BadRequest("bad"))
    assert len(sent) == 1
    assert sent[0][0] == ADMIN


def test_no_admin_configured_never_sends():
    handler = observability.make_error_handler(0)
    sent = _run(handler, object(), BadRequest("bad"))
    assert sent == []
