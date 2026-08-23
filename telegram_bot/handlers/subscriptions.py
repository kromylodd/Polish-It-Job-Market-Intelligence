"""Subscriptions (Telegram Stars), admin grants/refunds, and the /premium menu.

This module also hosts the interactive premium menu router, which fans out to
the action helpers in ``premium_analytics`` and ``tracker_cmds`` — the only
cross-handler imports in the package (kept one-directional to avoid cycles).
"""

from __future__ import annotations

import asyncio
import datetime
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice, Update
from telegram.ext import ContextTypes

from telegram_bot import payments
from telegram_bot.analytics import log_command
from telegram_bot.handlers.common import (
    ADMIN_CHAT_ID,
    PREMIUM_FREE,
    WARSAW_TZ,
    _esc,
    _upsell_text,
    has_feature,
    load_config,
)
from telegram_bot.handlers.premium_analytics import _do_export, _do_report, _do_trend_overall
from telegram_bot.handlers.tracker_cmds import _do_mytracker

logger = logging.getLogger(__name__)


async def cmd_subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    log_command(update.effective_chat.id, "subscribe")
    if PREMIUM_FREE:
        await update.message.reply_text(
            "📊 <b>Everything is free right now</b>\n\n"
            "All analytics &amp; tools are currently open to everyone at no cost — "
            "no subscription needed. Open /premium to use them.",
            parse_mode="HTML",
        )
        return
    await _show_subscribe_menu(context, update.effective_chat.id)


async def _show_subscribe_menu(context, chat_id: int):
    """Show tiers and let the user buy one with Telegram Stars."""
    sub = payments.get_subscription(chat_id)

    lines = ["💎 <b>Premium tiers</b>\n"]
    if sub:
        exp = datetime.datetime.fromtimestamp(sub["expires_at"], tz=WARSAW_TZ).strftime("%Y-%m-%d")
        if sub.get("in_grace"):
            lines.append(
                f"⏳ <b>{payments.TIERS[sub['tier']]['name']}</b> expired {exp} — "
                f"in grace period, renew to keep access\n"
            )
        else:
            lines.append(f"✅ Active: <b>{payments.TIERS[sub['tier']]['name']}</b> until {exp}\n")
    lines.append("<b>Free</b> — daily digest + all filters (no cost)\n")
    for t in payments.TIERS.values():
        lines.append(f"<b>{t['name']} — {t['stars']} ⭐ / 30 days</b>\n{t['blurb']}\n")
    lines.append("<i>Payments use Telegram Stars. Tap a button below to subscribe.</i>")

    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(f"{t['name']} — {t['stars']} ⭐", callback_data=f"subtier:{key}")]
            for key, t in payments.TIERS.items()
        ]
    )
    await context.bot.send_message(
        chat_id, "\n".join(lines), parse_mode="HTML", reply_markup=keyboard
    )


async def _send_invoice(context: ContextTypes.DEFAULT_TYPE, chat_id: int, tier: str):
    """Send a Telegram Stars invoice for a subscription tier."""
    t = payments.TIERS[tier]
    await context.bot.send_invoice(
        chat_id=chat_id,
        title=f"{t['name']} subscription — 30 days",
        description=t["blurb"],
        payload=payments.make_payload(tier, chat_id),
        provider_token="",  # empty string ⇒ pay with Telegram Stars
        currency="XTR",
        prices=[LabeledPrice(f"{t['name']} (30 days)", t["stars"])],
    )


async def precheckout_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Approve the pre-checkout if the payload maps to a known tier."""
    query = update.pre_checkout_query
    if payments.tier_for_payload(query.invoice_payload):
        await query.answer(ok=True)
    else:
        await query.answer(ok=False, error_message="This subscription is no longer available.")


async def successful_payment_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Activate the subscription after a successful Stars payment."""
    sp = update.message.successful_payment
    tier = payments.tier_for_payload(sp.invoice_payload)
    if not tier:
        return
    chat_id = update.effective_chat.id
    expires = payments.record_and_activate(
        sp.telegram_payment_charge_id, chat_id, tier, sp.total_amount
    )
    if expires is None:
        # Duplicate/redelivered payment event — already processed, don't re-grant.
        logger.info(
            "Duplicate successful_payment ignored: chat=%s charge_id=%s",
            chat_id,
            sp.telegram_payment_charge_id,
        )
        return
    payments.invalidate_subscription_cache(chat_id)
    logger.info(
        "Payment ok: chat=%s tier=%s stars=%s charge_id=%s",
        chat_id,
        tier,
        sp.total_amount,
        sp.telegram_payment_charge_id,
    )
    exp = datetime.datetime.fromtimestamp(expires).strftime("%Y-%m-%d")
    await update.message.reply_text(
        f"🎉 <b>{payments.TIERS[tier]['name']}</b> is active until {exp}!\n\n"
        "Try /report, /skills Python, /salary Python, or /latest.",
        parse_mode="HTML",
    )


# --- Admin: refunds & grants ---


async def cmd_refund(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin: refund a Telegram Stars charge and revoke the subscription.

    Usage: /refund <telegram_payment_charge_id>. The charge id is logged on the
    successful_payment event and shown in the payments table.
    """
    if update.effective_chat.id != ADMIN_CHAT_ID:
        return
    log_command(update.effective_chat.id, "refund")
    args = context.args or []
    if not args:
        await update.message.reply_text("Usage: /refund <charge_id>")
        return
    charge_id = args[0].strip()
    pay = await asyncio.to_thread(payments.get_payment, charge_id)
    if not pay:
        await update.message.reply_text(f"No payment found for charge {charge_id}.")
        return
    if pay.get("refunded_at"):
        await update.message.reply_text("That charge was already refunded.")
        return
    # Issue the actual Stars refund via Telegram, then record it locally.
    try:
        await context.bot.refund_star_payment(
            user_id=int(pay["chat_id"]), telegram_payment_charge_id=charge_id
        )
    except Exception as e:
        logger.warning("refund_star_payment failed for %s: %s", charge_id, e)
        await update.message.reply_text(f"Telegram refund failed: {e}")
        return
    await asyncio.to_thread(payments.refund_payment, charge_id)
    try:
        await context.bot.send_message(
            int(pay["chat_id"]),
            "↩️ Your subscription was refunded. Premium access has ended — "
            "you can re-subscribe anytime with /subscribe.",
        )
    except Exception:
        pass
    await update.message.reply_text(
        f"✅ Refunded {pay['stars']} ⭐ to chat {pay['chat_id']} and revoked {pay['tier']}."
    )


async def cmd_givepremium(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin: grant premium (Pro tier) to a user by chat_id.

    Usage:
      /givepremium <chat_id>           — 30 days (default)
      /givepremium <chat_id> 90        — 90 days
      /givepremium <chat_id> forever   — ~100 years (effectively permanent)
    """
    if update.effective_chat.id != ADMIN_CHAT_ID:
        return
    log_command(update.effective_chat.id, "givepremium")
    args = context.args or []
    if not args:
        await update.message.reply_text(
            "Usage: <code>/givepremium &lt;chat_id&gt; [days|forever]</code>\n\n"
            "Examples:\n"
            "<code>/givepremium 123456789</code> — 30 days\n"
            "<code>/givepremium 123456789 90</code> — 90 days\n"
            "<code>/givepremium 123456789 forever</code> — permanent",
            parse_mode="HTML",
        )
        return

    try:
        target_chat_id = int(args[0])
    except ValueError:
        await update.message.reply_text("⚠️ First argument must be a numeric chat ID.")
        return

    # Determine duration
    days = 30  # default
    duration_label = "30 days"
    if len(args) > 1:
        dur = args[1].lower()
        if dur in ("forever", "permanent", "infinite", "навсегда"):
            days = 36500  # ~100 years
            duration_label = "forever ♾️"
        else:
            try:
                days = int(dur)
                duration_label = f"{days} days"
            except ValueError:
                await update.message.reply_text(
                    "⚠️ Second argument must be a number of days or 'forever'."
                )
                return

    expires = await asyncio.to_thread(payments.activate, target_chat_id, "pro", days=days)
    exp_str = datetime.datetime.fromtimestamp(expires).strftime("%Y-%m-%d")

    # Notify the recipient (best-effort)
    try:
        await context.bot.send_message(
            target_chat_id,
            f"🎁 You've been granted <b>Pro</b> access ({duration_label})!\n"
            f"Expires: {exp_str}\n\n"
            "Enjoy all premium features — see /premium for the full menu.",
            parse_mode="HTML",
        )
    except Exception:
        pass  # User may have blocked the bot

    await update.message.reply_text(
        f"✅ Granted <b>Pro</b> to <code>{target_chat_id}</code> for {duration_label}.\n"
        f"   Expires: {exp_str}",
        parse_mode="HTML",
    )


async def cmd_revokepremium(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin: revoke premium from a user by chat_id.

    Usage: /revokepremium <chat_id>
    """
    if update.effective_chat.id != ADMIN_CHAT_ID:
        return
    log_command(update.effective_chat.id, "revokepremium")
    args = context.args or []
    if not args:
        await update.message.reply_text(
            "Usage: <code>/revokepremium &lt;chat_id&gt;</code>",
            parse_mode="HTML",
        )
        return

    try:
        target_chat_id = int(args[0])
    except ValueError:
        await update.message.reply_text("⚠️ Argument must be a numeric chat ID.")
        return

    revoked = await asyncio.to_thread(payments.revoke_subscription, target_chat_id)
    if revoked:
        # Notify the user (best-effort)
        try:
            await context.bot.send_message(
                target_chat_id,
                "⚠️ Your premium access has been revoked by the admin.\n"
                "You can re-subscribe anytime with /subscribe.",
            )
        except Exception:
            pass
        await update.message.reply_text(
            f"✅ Revoked premium from <code>{target_chat_id}</code>.",
            parse_mode="HTML",
        )
    else:
        await update.message.reply_text(
            f"ℹ️ <code>{target_chat_id}</code> has no active subscription.",
            parse_mode="HTML",
        )


# --- Callback routers for premium inline buttons ---


async def _callback_subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle tier-selection buttons (subtier:<tier>) by sending an invoice."""
    query = update.callback_query
    await query.answer()
    tier = query.data.split(":", 1)[1] if ":" in query.data else ""
    if tier not in payments.TIERS:
        return
    try:
        await _send_invoice(context, query.message.chat.id, tier)
    except Exception as e:
        logger.error("send_invoice failed: %s", e)
        await context.bot.send_message(
            query.message.chat.id,
            "⚠️ Could not start checkout. Telegram Stars payments may not be enabled "
            "for this bot yet.",
        )


# --- Premium sub-menu (interactive, like /filters) ---


def _build_premium_menu(chat_id: int) -> tuple[str, InlineKeyboardMarkup]:
    """Build the premium menu text + inline keyboard."""
    sub = payments.get_subscription(chat_id)
    if chat_id == ADMIN_CHAT_ID:
        status = "👑 Admin (all features)"
    elif sub:
        tier_name = payments.TIERS[sub["tier"]]["name"]
        exp_dt = datetime.datetime.fromtimestamp(sub["expires_at"], tz=WARSAW_TZ)
        exp_str = exp_dt.strftime("%Y-%m-%d")
        days_left = (exp_dt - datetime.datetime.now(tz=WARSAW_TZ)).days
        if sub.get("in_grace"):
            status = (
                f"⏳ {tier_name} expired {exp_str} — in grace period "
                f"({payments.GRACE_DAYS}d), renew to keep access"
            )
        else:
            days_txt = "expires today" if days_left <= 0 else f"{days_left}d left"
            status = f"✅ {tier_name} active — expires {exp_str} ({days_txt})"
    else:
        status = "📊 All analytics & tools are free right now"

    text = (
        "📊 <b>Analytics &amp; tools</b>\n"
        f"<i>{status}</i>\n\n"
        "Insights built on the live market data. Tap an item — the ones "
        "that need a keyword will show you how.\n\n"
        "<i>Filters &amp; daily alerts are free too.</i>"
    )
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("💰 Salary insights", callback_data="prem:salary"),
                InlineKeyboardButton("🧩 Skill pairs", callback_data="prem:skills"),
            ],
            [
                InlineKeyboardButton("📈 Market trend", callback_data="prem:trend"),
                InlineKeyboardButton("🏢 Company intel", callback_data="prem:company"),
            ],
            [
                InlineKeyboardButton("📊 Weekly report", callback_data="prem:report"),
                InlineKeyboardButton("📦 Export CSV", callback_data="prem:export"),
            ],
            [
                InlineKeyboardButton("📋 My tracker", callback_data="prem:tracker"),
            ],
        ]
    )
    return text, keyboard


async def cmd_premium(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show the interactive premium menu."""
    log_command(update.effective_chat.id, "premium")
    text, keyboard = _build_premium_menu(update.effective_chat.id)
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=keyboard)


# Keyword-driven items: tapping shows usage (they need a text argument).
_PREMIUM_PROMPTS = {
    "salary": (
        "💰 <b>Salary insights</b>\n\n"
        "Send:\n<code>/salary Python</code>\n<code>/salary Python senior</code>\n\n"
        "Min / median / max + a per-seniority breakdown.",
        payments.FEATURE_SALARY,
        "Salary insights",
    ),
    "skills": (
        "🧩 <b>Skill co-occurrence</b>\n\n"
        "Send:\n<code>/skills Python</code>\n\n"
        "The technologies most often requested together with it.",
        payments.FEATURE_SKILLS,
        "/skills co-occurrence",
    ),
    "company": (
        "🏢 <b>Company intel</b>\n\n"
        "Send:\n<code>/company Allegro</code>\n\n"
        "Listing count, salary range and sample roles.",
        payments.FEATURE_COMPANY,
        "Company intel",
    ),
}


async def _callback_premium(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Route premium-menu button taps (prem:<action>)."""
    query = update.callback_query
    action = query.data.split(":", 1)[1] if ":" in query.data else ""
    chat_id = query.message.chat.id
    back = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="prem:home")]])

    # Back to the menu.
    if action == "home":
        await query.answer()
        text, keyboard = _build_premium_menu(chat_id)
        try:
            await query.edit_message_text(text, parse_mode="HTML", reply_markup=keyboard)
        except Exception:
            # Originating message may be a photo (e.g. /trend chart) which can't
            # be edited into text — send a fresh menu instead.
            await context.bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=keyboard)
        return

    # Keyword-driven items → show usage (respect the paywall).
    if action in _PREMIUM_PROMPTS:
        await query.answer()
        msg, feature, label = _PREMIUM_PROMPTS[action]
        if not has_feature(chat_id, feature):
            msg = _upsell_text(label)
        await query.edit_message_text(msg, parse_mode="HTML", reply_markup=back)
        return

    # /myskills is free — show current + usage.
    if action == "myskills":
        await query.answer()
        skills = load_config(chat_id).get("skills", [])
        current = ", ".join(skills) if skills else "none set"
        msg = (
            f"🎯 <b>Your skills:</b> {_esc(current)}\n\n"
            "Set: <code>/myskills python sql airflow</code>\n"
            "Clear: <code>/myskills clear</code>\n\n"
            "<i>Ranks your /latest results by % match. (Free)</i>"
        )
        await query.edit_message_text(msg, parse_mode="HTML", reply_markup=back)
        return

    # Action items → run and send results as new messages.
    await query.answer()
    if action == "trend":
        await _do_trend_overall(context, chat_id)
    elif action == "report":
        await _do_report(context, chat_id)
    elif action == "export":
        await _do_export(context, chat_id)
    elif action == "tracker":
        await _do_mytracker(context, chat_id)
    elif action == "subscribe":
        await _show_subscribe_menu(context, chat_id)
