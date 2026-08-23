"""Core / onboarding & meta handlers: /start, /help, /privacy, /feedback."""

from __future__ import annotations

from telegram import Update
from telegram.ext import ContextTypes

from telegram_bot import payments
from telegram_bot.analytics import (
    is_opted_out,
    log_command,
    log_filter_choice,
    set_opt_out,
)
from telegram_bot.handlers.common import ADMIN_CHAT_ID, load_config


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    is_new = log_command(update.effective_chat.id, "start")
    chat_id = update.effective_chat.id

    # Log default preferences for new users so they count in statistics
    if is_new:
        config = load_config(chat_id)
        log_filter_choice(chat_id, "seniority", config.get("seniorities", []))
        log_filter_choice(chat_id, "category", config.get("categories", []))
        log_filter_choice(chat_id, "workplace", config.get("workplace_types", []))
        log_filter_choice(chat_id, "employment", config.get("employment_types", []))
    text = (
        "👋 <b>Polish IT Job Market Intelligence</b>\n\n"
        "I send you daily alerts for IT job listings from justjoin.it "
        "matching your personal filters.\n\n"
        "<b>🎛️ Filter dimensions:</b>\n"
        "• Seniority (intern/junior/mid/senior/lead)\n"
        "• Technologies (Python, React, Docker, etc.)\n"
        "• Category (python, java, devops, data, mobile...)\n"
        "• Workplace (remote/hybrid/office)\n"
        "• Employment (B2B/UoP/zlecenie)\n"
        "• Salary minimum\n"
        "• City\n\n"
        "<b>🎯 Tolerance:</b> Set how many filters can mismatch.\n"
        "E.g. tolerance=1 means if everything matches except one "
        "filter, the listing still appears.\n\n"
        "<b>Commands:</b> /help\n\n"
        "<i>📊 This bot collects anonymous usage statistics (command popularity, "
        "popular filters) to improve the service. No personal data is stored — "
        "see /privacy.</i>"
    )

    # New users: point them at the (now free) analytics commands.
    if is_new:
        payments.activate_trial(chat_id)
        text += (
            "\n\n📊 <b>All analytics are free right now.</b>\n"
            "Try /salary Python, /trend, /skills — or open /premium for the full menu."
        )

    await update.message.reply_text(text, parse_mode="HTML")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    log_command(update.effective_chat.id, "help")
    text = (
        "<b>Commands:</b>\n\n"
        "<b>Main:</b>\n"
        "🔍 /latest — get recent matching listings 💎\n"
        "📋 /filters — view &amp; edit all filters\n"
        "⚙️ /tolerance — set mismatch tolerance\n"
        "🧠 /myskills — save your skills (ranks results, no filtering)\n"
        "🔒 /privacy — data collection settings\n"
        "💬 /feedback — send a suggestion or bug report\n\n"
        "<b>Filter commands</b> (edit via /filters or directly):\n"
        "  /seniority — junior, mid, senior, lead\n"
        "  /tech — filter by technology (or /tech list)\n"
        "  /category — job categories (or /category list)\n"
        "  /workplace — remote / hybrid / office\n"
        "  /employment — b2b / uop / zlecenie\n"
        "  /salary — minimum salary\n"
        "  /city — location filter\n\n"
        "<b>📊 Analytics &amp; tools</b> — open the menu with /premium:\n"
        "  /salary &lt;tech&gt; — salary by contract type (UoP vs B2B)\n"
        "  /trend [tech] — market &amp; demand trends\n"
        "  /skills &lt;tech&gt; — co-occurring technologies\n"
        "  /company &lt;name&gt; — company hiring intel\n"
        "  /report — weekly market report\n"
        "  /export — download your listings (CSV)\n"
        "  /applied /interested /rejected — track jobs\n"
        "  /mytracker — your tracked applications\n\n"
        "<i>Empty filters = no restriction on that dimension.\n"
        "Only active filters count toward tolerance.\n"
        "All features are currently free for everyone.</i>"
    )
    await update.message.reply_text(text, parse_mode="HTML")


async def cmd_privacy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show privacy/data collection info with opt-out toggle."""
    log_command(update.effective_chat.id, "privacy")
    args = context.args
    chat_id = update.effective_chat.id

    # Handle toggle: /privacy off or /privacy on
    if args and args[0].lower() in ("off", "disable", "optout"):
        set_opt_out(chat_id, True)
        await update.message.reply_text(
            "✅ <b>Data collection disabled.</b>\n\n"
            "Only your existence as a user (+1 count) is tracked.\n"
            "No commands, filter preferences, or usage patterns are recorded.\n\n"
            "Re-enable anytime: /privacy on",
            parse_mode="HTML",
        )
        return

    if args and args[0].lower() in ("on", "enable", "optin"):
        set_opt_out(chat_id, False)
        await update.message.reply_text(
            "✅ <b>Data collection enabled.</b>\n\n"
            "Anonymous usage stats will be collected to help improve the bot.\n"
            "Thank you for helping! 🙏\n\n"
            "Disable anytime: /privacy off",
            parse_mode="HTML",
        )
        return

    # Show info
    opted_out = is_opted_out(chat_id)
    status = "🔴 Disabled" if opted_out else "🟢 Enabled"

    text = (
        "<b>🔒 Privacy &amp; Data Collection</b>\n\n"
        f"<b>Your status:</b> {status}\n"
        f"<b>Your chat ID:</b> <code>{chat_id}</code>\n\n"
        "This bot collects <b>anonymous usage statistics</b> to understand "
        "what the Polish IT market needs and improve the service.\n\n"
        "<b>When enabled, we track:</b>\n"
        "• Which commands are used (and how often)\n"
        "• Which filters are popular (technologies, categories, cities)\n"
        "• This helps us know what features to build next\n\n"
        "<b>When disabled, we only track:</b>\n"
        "• +1 to the total user count (nothing else)\n\n"
        "<b>How your identity is protected:</b>\n"
        "• Your chat ID is hashed (salted HMAC-SHA256) before storage —\n"
        "  the raw ID is never written to disk\n"
        "• No name, username, or profile info is ever collected\n"
        "• Regular command/filter usage never stores message text\n\n"
        "<b>One exception — /feedback:</b>\n"
        "• Text you send via /feedback is stored and forwarded to the admin\n"
        "  (that's the whole point of the command), so don't include secrets\n\n"
        "<b>Toggle:</b>\n"
        "  /privacy off — disable detailed tracking\n"
        "  /privacy on — re-enable (helps improve the bot! 🙏)"
    )
    await update.message.reply_text(text, parse_mode="HTML")


async def cmd_feedback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /feedback — users can send suggestions or complaints."""
    log_command(update.effective_chat.id, "feedback")
    args = context.args

    if not args:
        await update.message.reply_text(
            "💬 <b>Feedback</b>\n\n"
            "Have a suggestion, bug report, or complaint?\n\n"
            "<b>Usage:</b>\n"
            "<code>/feedback Your message here</code>\n\n"
            "<i>Your feedback is sent anonymously to the bot admin.</i>",
            parse_mode="HTML",
        )
        return

    # Store feedback
    feedback_text = " ".join(args)
    _save_feedback(update.effective_chat.id, feedback_text)

    # Forward to admin (reuse the running bot instance)
    if ADMIN_CHAT_ID:
        try:
            await context.bot.send_message(
                chat_id=ADMIN_CHAT_ID,
                text=f"💬 <b>New feedback:</b>\n\n{feedback_text}",
                parse_mode="HTML",
            )
        except Exception:
            pass  # Don't fail the user's command if admin notify fails

    await update.message.reply_text(
        "✅ Thanks for your feedback! It's been sent to the admin.\n\n"
        "<i>We read every message and use it to improve the bot.</i>",
        parse_mode="HTML",
    )


def _save_feedback(chat_id: int, text: str):
    """Save feedback to the analytics DB (preserved across /analytics reset)."""
    from telegram_bot.analytics import log_feedback

    log_feedback(chat_id, text)
