"""Premium analytics & tools: /skills, /trend, /company, /export, /report.

The ``_do_*`` helpers take a bare ``chat_id`` so they can be driven both by their
slash command and by the inline premium menu in ``subscriptions``.
"""

from __future__ import annotations

import asyncio
import csv
import datetime
import io
import logging

from telegram import Update
from telegram.ext import ContextTypes

from telegram_bot import payments, reports, serving
from telegram_bot.analytics import log_command
from telegram_bot.handlers.common import (
    _esc,
    _get_latest_listings,
    _premium_back_kb,
    _require_feature,
    _require_feature_chat,
    load_config,
)

logger = logging.getLogger(__name__)


async def cmd_skills(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Premium: technologies that co-occur with a given tech, with percentages."""
    if not await _require_feature(update, payments.FEATURE_SKILLS, "/skills co-occurrence"):
        return
    log_command(update.effective_chat.id, "skills")
    if not context.args:
        await update.message.reply_text(
            "🧩 <b>Skill co-occurrence</b>\n\n"
            "Usage: <code>/skills Python</code>\n"
            "<i>Shows which technologies are most often requested alongside it.</i>",
            parse_mode="HTML",
        )
        return

    tech = " ".join(context.args)
    data = await asyncio.to_thread(serving.skills_for_tech, tech)
    if not data or not data.get("related"):
        await update.message.reply_text(
            f"📭 No co-occurrence data for <b>{_esc(tech)}</b> yet.",
            parse_mode="HTML",
        )
        return

    lines = [f"🧩 <b>Often requested with {_esc(tech)}</b>"]
    if data.get("total"):
        lines.append(f"<i>Across {data['total']} listings mentioning {_esc(tech)}</i>")
    lines.append("")
    for r in data["related"]:
        pct = f" — {r['pct']}%" if r.get("pct") is not None else ""
        lines.append(f"• {_esc(r['tech'])}: {r['count']} listings{pct}")
    await update.message.reply_text(
        "\n".join(lines), parse_mode="HTML", reply_markup=_premium_back_kb()
    )


async def cmd_trend(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Premium: overall market trend, or weekly demand for a specific technology."""
    if not await _require_feature(update, payments.FEATURE_TREND, "Market trends"):
        return
    log_command(update.effective_chat.id, "trend")

    if context.args:
        tech = context.args[0]
        data = await asyncio.to_thread(serving.tech_demand_trend, tech, 12)
        if not data:
            await update.message.reply_text(
                f"📭 No demand trend for <b>{_esc(tech)}</b> yet.", parse_mode="HTML"
            )
            return
        latest = data[-1]
        wow = latest.get("wow_change") or 0
        arrow = "🔺" if wow > 0 else ("🔻" if wow < 0 else "▶️")
        caption = (
            f"📈 <b>{_esc(tech)} — weekly demand</b>\n"
            f"Latest week: {latest.get('listing_count')} listings "
            f"({arrow} {'+' if wow >= 0 else ''}{wow} WoW)"
        )
        png = await asyncio.to_thread(reports.build_tech_demand_chart, tech)
        if png:
            await update.message.reply_photo(
                png, caption=caption, parse_mode="HTML", reply_markup=_premium_back_kb()
            )
        else:
            await update.message.reply_text(
                caption, parse_mode="HTML", reply_markup=_premium_back_kb()
            )
        return

    # No tech → overall market trend.
    await _do_trend_overall(context, update.effective_chat.id)


async def _do_trend_overall(context, chat_id: int):
    """Send the overall 8-week market trend (chart + summary)."""
    if not await _require_feature_chat(context, chat_id, payments.FEATURE_TREND, "Market trends"):
        return
    trend = await asyncio.to_thread(serving.market_trend, 8)
    if not trend:
        await context.bot.send_message(
            chat_id,
            "📭 Market trend data isn't ready yet — try again after the next pipeline run.",
        )
        return
    latest, first = trend[-1], trend[0]
    vol_now = latest.get("rolling_7d_listings")
    vol_then = first.get("rolling_7d_listings")
    sal_now = latest.get("rolling_7d_avg_salary")
    lines = ["📈 <b>Market trend (last 8 weeks)</b>\n"]
    if vol_now is not None and vol_then:
        delta = vol_now - vol_then
        arrow = "🔺" if delta > 0 else ("🔻" if delta < 0 else "▶️")
        lines.append(f"{arrow} 7-day volume: {vol_then} → {vol_now}")
    if sal_now:
        lines.append(f"💰 Avg salary (rolling): ~{round(sal_now)} PLN")
    png = await asyncio.to_thread(reports.build_trend_chart)
    if png:
        await context.bot.send_photo(
            chat_id,
            png,
            caption="\n".join(lines),
            parse_mode="HTML",
            reply_markup=_premium_back_kb(),
        )
    else:
        await context.bot.send_message(
            chat_id, "\n".join(lines), parse_mode="HTML", reply_markup=_premium_back_kb()
        )


async def cmd_company(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Premium: how many current listings a company has + salary range + samples."""
    if not await _require_feature(update, payments.FEATURE_COMPANY, "Company intel"):
        return
    log_command(update.effective_chat.id, "company")
    if not context.args:
        await update.message.reply_text(
            "🏢 <b>Company intel</b>\n\n"
            "Usage:\n"
            "<code>/company Allegro</code> — intel on a specific company\n"
            "<code>/company list</code> — top companies by number of offers",
            parse_mode="HTML",
        )
        return

    if context.args[0].lower() == "list":
        companies = await asyncio.to_thread(serving.top_hiring_companies, 20)
        if not companies:
            await update.message.reply_text("📭 No company data available yet.")
            return
        lines = ["🏢 <b>Top Companies by Offers</b>\n"]
        for i, c in enumerate(companies, 1):
            name = c.get("company_name", "?")
            count = c.get("listing_count", 0)
            lines.append(f"{i}. <b>{_esc(name)}</b> — {count} listings")
        lines.append("\n<i>Use <code>/company Name</code> for details on any company.</i>")
        await update.message.reply_text(
            "\n".join(lines), parse_mode="HTML", reply_markup=_premium_back_kb()
        )
        return

    name = " ".join(context.args)
    data = await asyncio.to_thread(serving.company_intel, name)
    if not data:
        await update.message.reply_text(
            f"📭 No current listings found for <b>{_esc(name)}</b>.", parse_mode="HTML"
        )
        return

    cur = data.get("currency", "PLN")
    lines = [f"🏢 <b>{_esc(name)}</b>", f"<i>{data['listing_count']} current listings</i>", ""]
    if data.get("avg_min") and data.get("avg_max"):
        lines.append(f"💰 Avg range: {data['avg_min']}–{data['avg_max']} {cur}")
    if data.get("sample_titles"):
        lines.append("\n<b>Sample roles:</b>")
        for t in data["sample_titles"]:
            lines.append(f"• {_esc(t)}")
    await update.message.reply_text(
        "\n".join(lines), parse_mode="HTML", reply_markup=_premium_back_kb()
    )


def _listings_to_csv(listings: list[dict]) -> bytes:
    """Serialize listings to CSV bytes for /export."""
    fields = [
        "listing_id",
        "title",
        "company_name",
        "seniority",
        "employment_type",
        "workplace_type",
        "category",
        "salary_min",
        "salary_max",
        "currency",
        "cities",
        "technologies",
        "slug",
    ]
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    for listing in listings:
        row = dict(listing)
        for key in ("cities", "technologies"):
            val = row.get(key)
            if isinstance(val, list):
                row[key] = ", ".join(str(v) for v in val)
        writer.writerow(row)
    return buf.getvalue().encode("utf-8")


async def cmd_export(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _do_export(context, update.effective_chat.id)


async def _do_export(context, chat_id: int):
    """Export the user's filtered listings as a CSV file."""
    if not await _require_feature_chat(context, chat_id, payments.FEATURE_EXPORT, "/export"):
        return
    log_command(chat_id, "export")
    await context.bot.send_message(chat_id, "📦 Preparing your export…")

    config = load_config(chat_id)
    try:
        listings = await asyncio.to_thread(_get_latest_listings, config)
    except Exception as e:
        logger.error("/export failed: %s", e)
        await context.bot.send_message(chat_id, "⚠️ Could not build the export right now.")
        return

    if not listings:
        await context.bot.send_message(chat_id, "📭 No listings match your filters to export.")
        return

    csv_bytes = _listings_to_csv(listings)
    bio = io.BytesIO(csv_bytes)
    stamp = datetime.datetime.now().strftime("%Y%m%d")
    await context.bot.send_document(
        chat_id,
        document=bio,
        filename=f"listings_{stamp}.csv",
        caption=f"📄 {len(listings)} listings matching your filters",
    )


async def cmd_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _do_report(context, update.effective_chat.id)


async def _do_report(context, chat_id: int):
    """Weekly market report (top companies, hot tech, salary trend)."""
    if not await _require_feature_chat(
        context, chat_id, payments.FEATURE_REPORT, "Weekly market report"
    ):
        return
    log_command(chat_id, "report")
    await context.bot.send_message(chat_id, "📊 Building your market report…")

    text = await asyncio.to_thread(reports.build_market_report)
    png = await asyncio.to_thread(reports.build_trend_chart)
    if png:
        try:
            await context.bot.send_photo(chat_id, png, caption="Polish IT market — 8-week trend")
        except Exception as e:
            logger.warning("report chart send failed: %s", e)
    await context.bot.send_message(chat_id, text, parse_mode="HTML", disable_web_page_preview=True)
