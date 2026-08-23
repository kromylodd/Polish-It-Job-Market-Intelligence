"""Application tracker: /applied, /interested, /rejected, /mytracker.

Also owns the two inline callback routers for tracker buttons (``trk:``) and
tracker pagination (``trkpg:``), plus the message-rendering + snapshot-hydration
helpers they share.
"""

from __future__ import annotations

import asyncio
import html

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from telegram_bot import payments, serving, tracker
from telegram_bot.analytics import log_command
from telegram_bot.handlers.common import (
    _recall_listing,
    _require_feature,
    _require_feature_chat,
    has_feature,
)


def _extract_listing_id(token: str) -> str:
    """Derive a stable tracker key from a raw id or a justjoin.it URL."""
    token = token.strip()
    if token.startswith("http"):
        return token.rstrip("/").rsplit("/", 1)[-1] or token
    return token


async def _track_status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE, status: str):
    if not await _require_feature(update, payments.FEATURE_TRACKER, "Application tracker"):
        return
    log_command(update.effective_chat.id, status)
    args = context.args
    if not args:
        await update.message.reply_text(
            f"Usage: <code>/{status} &lt;listing-id or justjoin URL&gt;</code>\n\n"
            "💡 Easiest: tap the buttons under listings in /latest to track in one tap.",
            parse_mode="HTML",
        )
        return

    token = args[0]
    lid = _extract_listing_id(token)
    meta = _recall_listing(lid)
    url = meta.get("url") or (token if token.startswith("http") else None)
    ok = await asyncio.to_thread(
        tracker.set_status,
        update.effective_chat.id,
        lid,
        status,
        title=meta.get("title"),
        company=meta.get("company"),
        url=url,
    )
    if ok:
        await update.message.reply_text(
            f"✅ Marked as <b>{status}</b>. See /mytracker", parse_mode="HTML"
        )
    else:
        await update.message.reply_text("⚠️ Could not save that — try again.")


async def cmd_applied(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _track_status_cmd(update, context, "applied")


async def cmd_interested(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _track_status_cmd(update, context, "interested")


async def cmd_rejected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _track_status_cmd(update, context, "rejected")


async def cmd_mytracker(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _do_mytracker(context, update.effective_chat.id, status_filter=None, page=0)


_TRACKER_PAGE_SIZE = 10


def _build_tracker_message(
    page_rows: list[dict], c: dict[str, int], status_filter: str | None, page: int, total: int
) -> tuple[str, InlineKeyboardMarkup]:
    """Build paginated tracker text + navigation keyboard.

    ``page_rows`` is already the current page's slice (from tracker.list_page);
    ``total`` is the full count for the active filter.
    """
    icon = {"applied": "✅", "interested": "👀", "rejected": "❌"}
    total_pages = max(1, (total + _TRACKER_PAGE_SIZE - 1) // _TRACKER_PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))

    # Header with totals
    filter_label = (
        f" ({icon.get(status_filter, '')} {status_filter})" if status_filter else " (all)"
    )
    header = (
        f"📋 <b>Your tracker</b>{filter_label} — "
        f"✅ {c.get('applied', 0)} · 👀 {c.get('interested', 0)} · ❌ {c.get('rejected', 0)}\n"
    )
    lines = [header]

    for a in page_rows:
        if a.get("title"):
            title = html.escape(str(a["title"]))
            company = f" @ {html.escape(str(a['company']))}" if a.get("company") else ""
            link = f"\n   {a['url']}" if a.get("url") else ""
        else:
            # Metadata never captured and the offer is no longer in the snapshot
            # (de-listed / aged out). Show a readable placeholder instead of a
            # raw listing id the user can't make sense of.
            title = "<i>Offer no longer available</i>"
            company = ""
            link = ""
        lines.append(f"{icon.get(a['status'], '•')} {title}{company}{link}")

    if total > _TRACKER_PAGE_SIZE:
        lines.append(f"\n<i>Page {page + 1}/{total_pages} ({total} total)</i>")

    # Build keyboard: status filter row + pagination row
    sf = status_filter or "all"
    filter_buttons = []
    for label, val in [("All", "all"), ("✅", "applied"), ("👀", "interested"), ("❌", "rejected")]:
        marker = "• " if sf == val else ""
        filter_buttons.append(
            InlineKeyboardButton(f"{marker}{label}", callback_data=f"trkpg:{val}:0")
        )

    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton("◀️ Prev", callback_data=f"trkpg:{sf}:{page - 1}"))
    if page < total_pages - 1:
        nav_buttons.append(InlineKeyboardButton("Next ▶️", callback_data=f"trkpg:{sf}:{page + 1}"))

    rows = [filter_buttons]
    if nav_buttons:
        rows.append(nav_buttons)

    return "\n".join(lines), InlineKeyboardMarkup(rows)


def _hydrate_tracker_rows(chat_id: int, rows: list[dict]) -> list[dict]:
    """Fill in missing title/company/url for tracked rows from the live snapshot.

    Rows tracked via an inline button while the listing wasn't in the in-memory
    cache (bot restarted or LRU-evicted between showing the listing and the tap)
    get saved with NULL metadata, so the tracker would otherwise render a raw
    listing id. Here we recover the offer's title/link from the current market
    snapshot and persist it back to the tracker DB so the fix is permanent and
    the lookup only happens once. Offers that have aged out of the snapshot stay
    without metadata and are shown as a friendly placeholder by the renderer.
    """
    for a in rows:
        if a.get("title"):
            continue
        lid = a.get("listing_id")
        meta = serving.listing_meta(lid) if lid else None
        if not meta or not meta.get("title"):
            continue
        a["title"] = meta.get("title")
        a["company"] = meta.get("company")
        a["url"] = meta.get("url")
        # Persist so future views are instant and survive restarts.
        tracker.set_status(
            chat_id,
            lid,
            a["status"],
            title=meta.get("title"),
            company=meta.get("company"),
            url=meta.get("url"),
        )
    return rows


async def _do_mytracker(context, chat_id: int, *, status_filter: str | None = None, page: int = 0):
    """Show the user's tracked applications (paginated)."""
    if not await _require_feature_chat(
        context, chat_id, payments.FEATURE_TRACKER, "Application tracker"
    ):
        return
    log_command(chat_id, "mytracker")
    page = max(0, page)
    c = await asyncio.to_thread(tracker.counts, chat_id)
    page_rows, total = await asyncio.to_thread(
        tracker.list_page, chat_id, status_filter, _TRACKER_PAGE_SIZE, page * _TRACKER_PAGE_SIZE
    )
    if total == 0:
        msg = (
            "📋 Your tracker is empty.\n"
            if not status_filter
            else f"📋 No <b>{status_filter}</b> offers.\n"
        )
        await context.bot.send_message(
            chat_id,
            msg + "Tap the buttons under /latest listings, or use /applied &lt;id&gt;.",
            parse_mode="HTML",
        )
        return

    page_rows = await asyncio.to_thread(_hydrate_tracker_rows, chat_id, page_rows)
    text, keyboard = _build_tracker_message(page_rows, c, status_filter, page, total)
    await context.bot.send_message(
        chat_id, text, parse_mode="HTML", disable_web_page_preview=True, reply_markup=keyboard
    )


async def _callback_tracker_page(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle tracker pagination/filter buttons (trkpg:<status>:<page>)."""
    query = update.callback_query
    parts = query.data.split(":", 2)
    if len(parts) != 3:
        await query.answer()
        return
    _, status_str, page_str = parts
    chat_id = query.message.chat.id

    if not has_feature(chat_id, payments.FEATURE_TRACKER):
        await query.answer("Premium feature — see /subscribe", show_alert=True)
        return

    status_filter = None if status_str == "all" else status_str
    page = int(page_str) if page_str.isdigit() else 0

    c = await asyncio.to_thread(tracker.counts, chat_id)
    page_rows, total = await asyncio.to_thread(
        tracker.list_page, chat_id, status_filter, _TRACKER_PAGE_SIZE, page * _TRACKER_PAGE_SIZE
    )

    if total == 0:
        await query.answer("No offers with that status")
        return

    page_rows = await asyncio.to_thread(_hydrate_tracker_rows, chat_id, page_rows)
    text, keyboard = _build_tracker_message(page_rows, c, status_filter, page, total)
    await query.answer()
    try:
        await query.edit_message_text(
            text, parse_mode="HTML", disable_web_page_preview=True, reply_markup=keyboard
        )
    except Exception:
        pass  # Message unchanged — Telegram raises if text is identical


async def _callback_tracker(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle tracker inline buttons (trk:<status>:<listing_id>)."""
    query = update.callback_query
    parts = query.data.split(":", 2)
    if len(parts) != 3:
        await query.answer()
        return
    _, status, lid = parts
    if not has_feature(query.message.chat.id, payments.FEATURE_TRACKER):
        await query.answer("Premium feature — see /subscribe", show_alert=True)
        return

    meta = _recall_listing(lid)
    await asyncio.to_thread(
        tracker.set_status,
        query.message.chat.id,
        lid,
        status,
        title=meta.get("title"),
        company=meta.get("company"),
        url=meta.get("url"),
    )
    labels = {"applied": "✅ Applied", "interested": "👀 Interested", "rejected": "❌ Rejected"}
    await query.answer(f"{labels.get(status, status)} — saved")
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass
