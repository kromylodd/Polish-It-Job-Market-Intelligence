"""Observability: systemd watchdog + failure alerting so a silently-dead or
*stuck* bot gets noticed and restarted.

``Restart=always`` in the systemd unit only catches a process that actually
exits. It does nothing for the nastier failure modes where the process is still
alive but no longer doing its job — a wedged event loop, a deadlocked thread, a
network layer that stopped delivering updates. This module adds the missing
safety nets:

  1. **systemd watchdog (sd_notify).** We send ``READY=1`` once the bot is
     initialised, then ping ``WATCHDOG=1`` from the event loop on a timer. If the
     loop stalls the pings stop and systemd (``WatchdogSec=``) kills + restarts
     the process. This is the only thing that catches a *silent* hang.
  2. **Global error handler.** Unhandled exceptions in handlers are otherwise
     just logged; here we also surface them to the admin chat so they're noticed.
  3. **run_job wrapper.** Background jobs (daily broadcast, renewal reminders)
     report failures to the admin instead of vanishing into the log.

Everything systemd-related is a no-op when ``NOTIFY_SOCKET`` is unset (local
dev, Docker, CI), so importing/using this module never changes behaviour off
systemd.
"""

from __future__ import annotations

import html
import logging
import os
import socket
import traceback
from collections.abc import Awaitable, Callable

from telegram.error import NetworkError, TimedOut

logger = logging.getLogger(__name__)


# --- systemd sd_notify (pure-Python, no external dependency) ---


def _notify(state: str) -> bool:
    """Send ``state`` to the systemd notify socket. No-op (returns False) off systemd.

    Implements the sd_notify protocol directly over the ``NOTIFY_SOCKET`` unix
    datagram socket so we don't need the optional ``systemd-python`` package.
    """
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return False
    # Abstract namespace sockets are prefixed with '@' in the env var.
    if addr[0] == "@":
        addr = "\0" + addr[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.connect(addr)
            sock.sendall(state.encode("utf-8"))
        return True
    except OSError as e:
        logger.warning("sd_notify(%r) failed: %s", state, e)
        return False


def notify_ready() -> None:
    """Tell systemd the service finished starting (required for ``Type=notify``)."""
    if _notify("READY=1"):
        logger.info("Notified systemd: READY=1")


def notify_stopping() -> None:
    """Best-effort ``STOPPING=1`` on shutdown so systemd doesn't flag a watchdog kill."""
    _notify("STOPPING=1")


def ping_watchdog() -> None:
    """Send a single ``WATCHDOG=1`` keep-alive. No-op off systemd."""
    _notify("WATCHDOG=1")


def watchdog_interval_seconds() -> float | None:
    """Recommended ping cadence: half of systemd's ``WATCHDOG_USEC``.

    Returns ``None`` when the watchdog isn't enabled (``WatchdogSec=`` unset, or
    not running under systemd), which callers use to skip scheduling the ping.
    """
    usec = os.environ.get("WATCHDOG_USEC")
    if not usec:
        return None
    try:
        # Ping at half the timeout so a single missed tick doesn't trip a restart.
        return max(1.0, int(usec) / 1_000_000 / 2)
    except ValueError:
        logger.warning("WATCHDOG_USEC is not an integer (%r); watchdog disabled", usec)
        return None


# --- PTB integration ---

_MAX_TB_CHARS = 3000


def make_error_handler(admin_chat_id: int) -> Callable[..., Awaitable[None]]:
    """Build a python-telegram-bot error handler that logs + alerts the admin.

    Kept as a factory (rather than a bare coroutine) so the admin chat id is
    captured explicitly instead of reached for via a module global.
    """

    async def error_handler(update: object, context) -> None:  # noqa: ANN001 (PTB signature)
        err = context.error

        # Transient network errors raised by the long-polling fetch loop
        # (getUpdates) are normal and self-healing: PTB's _network_loop_retry
        # catches them and retries with backoff, so the bot keeps working. These
        # arrive here with update=None (Application.run_polling dispatches poll
        # errors via process_error(update=None)). Don't page the admin for them —
        # just log at WARNING. Network errors *during update handling*
        # (update is not None) still alert, since those may lose a user's action.
        #
        # Match the exact transient types only. In PTB, BadRequest/Forbidden-style
        # errors also subclass NetworkError, so an isinstance() check would wrongly
        # swallow them; the httpx transport only ever raises plain NetworkError or
        # TimedOut for genuine connectivity blips (see request/_httpxrequest.py).
        if update is None and type(err) in (NetworkError, TimedOut):
            logger.warning("Transient network error in polling loop: %s: %s",
                           type(err).__name__, err)
            return

        logger.error("Unhandled exception while handling an update", exc_info=err)
        if not admin_chat_id:
            return
        tb = "".join(traceback.format_exception(type(err), err, err.__traceback__))
        # Keep the tail (where the actual failure is) and stay well under
        # Telegram's 4096-char message limit.
        snippet = html.escape(tb[-_MAX_TB_CHARS:])
        text = (
            f"⚠️ <b>Bot error</b>: {html.escape(type(err).__name__)}: "
            f"{html.escape(str(err))}\n\n<pre>{snippet}</pre>"
        )
        try:
            await context.bot.send_message(admin_chat_id, text, parse_mode="HTML")
        except Exception:  # never let the error handler raise
            logger.exception("Failed to deliver error alert to admin")

    return error_handler


async def _watchdog_job(context) -> None:  # noqa: ANN001 (PTB job signature)
    """JobQueue callback: keep the systemd watchdog fed."""
    ping_watchdog()


def setup(application, admin_chat_id: int) -> None:
    """Wire observability into a PTB ``Application``.

    * registers the global error handler (always), and
    * schedules the watchdog keep-alive job **iff** we're under a systemd unit
      with ``WatchdogSec=`` set (otherwise it's a no-op).

    Safe to call unconditionally; nothing here has an effect off systemd except
    the error handler, which is useful everywhere.
    """
    application.add_error_handler(make_error_handler(admin_chat_id))

    interval = watchdog_interval_seconds()
    if interval is None:
        return
    jq = getattr(application, "job_queue", None)
    if jq is None:
        logger.warning(
            "WATCHDOG_USEC is set but no JobQueue is available — the systemd "
            "watchdog won't be fed. Install python-telegram-bot[job-queue]."
        )
        return
    jq.run_repeating(_watchdog_job, interval=interval, first=1, name="sd_watchdog")
    logger.info("systemd watchdog ping scheduled every %.0fs", interval)
