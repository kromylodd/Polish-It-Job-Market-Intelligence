"""Admin-only usage analytics: /analytics (and its reset subcommand)."""

from __future__ import annotations

import asyncio

from telegram import Update
from telegram.ext import ContextTypes

from telegram_bot import config_store
from telegram_bot.analytics import (
    get_analytics_summary,
    is_opted_out,
    log_command,
    reset_analytics,
)
from telegram_bot.filters import ALL_CATEGORIES, ALL_SENIORITIES, ALL_WORKPLACES
from telegram_bot.handlers.common import ADMIN_CHAT_ID


def _current_filter_popularity() -> dict[str, dict[str, int]]:
    """How many users *currently* have each filter value selected, per dimension.

    Reads the authoritative per-user config store (current choices) rather than
    the append-only ``filter_choices`` event log. The event log double-counted
    the always-on defaults (seniority/category/workplace/employment are all
    selected by default and were re-logged on every /start), which is why those
    breakdowns showed inflated, misleading numbers. Counting distinct users from
    the live config fixes that and reflects de-selections too.

    Opted-out users are excluded to honour the privacy setting. Returns
    ``{dimension: {value: user_count}}`` with each dimension sorted by count desc.
    """
    store = config_store.load_local()
    dims = (
        "technologies",
        "categories",
        "cities",
        "seniorities",
        "workplace_types",
    )
    counts: dict[str, dict[str, int]] = {d: {} for d in dims}
    for cid, cfg in store.items():
        if not isinstance(cfg, dict):
            continue
        try:
            if is_opted_out(int(cid)):
                continue
        except (TypeError, ValueError):
            pass
        for d in dims:
            for val in cfg.get(d, []) or []:
                counts[d][val] = counts[d].get(val, 0) + 1
    return {
        d: dict(sorted(v.items(), key=lambda kv: kv[1], reverse=True)) for d, v in counts.items()
    }


async def cmd_analytics(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show aggregated bot usage analytics (admin only)."""
    if update.effective_chat.id != ADMIN_CHAT_ID:
        return
    log_command(update.effective_chat.id, "analytics")

    # Handle /analytics reset — wipe events + filter_choices
    args = context.args
    if args and args[0].lower() == "reset":
        result = await asyncio.to_thread(reset_analytics)
        await update.message.reply_text(
            "🗑️ <b>Analytics reset complete.</b>\n\n"
            f"• Events deleted: {result['events_deleted']}\n"
            f"• Filter choices deleted: {result['filter_choices_deleted']}\n\n"
            "<i>User count preserved. Counters start fresh from now.</i>",
            parse_mode="HTML",
        )
        return

    summary = get_analytics_summary()

    lines = ["<b>📊 Bot Usage Analytics</b>\n"]
    lines.append(f"👥 Total users: {summary['total_users']}")
    lines.append(
        f"   (📊 tracking: {summary['total_users'] - summary.get('opted_out_users', 0)}"
        f" · 🔒 opted out: {summary.get('opted_out_users', 0)})"
    )
    lines.append(f"📨 Total interactions: {summary['total_events']}\n")

    # Command usage
    cmds = summary.get("commands", {})
    if cmds:
        lines.append("<b>🔤 Command usage:</b>")
        for cmd, count in list(cmds.items())[:8]:
            lines.append(f"  /{cmd}: {count}")
        lines.append("")

    # Current user filter choices, counted once per user from the authoritative
    # config store (see _current_filter_popularity for why not the event log).
    pop = await asyncio.to_thread(_current_filter_popularity)

    def _section(title: str, dim: str, labels: dict | None = None, top: int | None = 7):
        values = pop.get(dim, {})
        if not values:
            return
        lines.append(f"<b>{title}</b>")
        items = list(values.items())[:top] if top else list(values.items())
        for val, n in items:
            label = labels.get(val, val) if labels else val
            lines.append(f"  {label}: {n} users")
        lines.append("")

    _section("💻 Technologies (users' filters):", "technologies")
    _section("📂 Categories (users' filters):", "categories", ALL_CATEGORIES)
    _section("🏙️ Cities (users' filters):", "cities")
    _section("🎯 Seniority (users' filters):", "seniorities", ALL_SENIORITIES, top=None)
    _section("🏠 Workplace (users' filters):", "workplace_types", ALL_WORKPLACES, top=None)

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")
