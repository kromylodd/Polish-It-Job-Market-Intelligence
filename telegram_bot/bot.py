"""
Interactive Telegram bot for Polish IT Job Market Intelligence.

Universal bot covering all IT roles with adjustable tolerance matching.

Commands:
    /start      — Welcome + overview
    /help       — All commands
    /filters    — Show current filter settings
    /seniority  — Set seniority filter
    /tech       — Set technology filter
    /category   — Set job category filter
    /workplace  — Set workplace type (remote/hybrid/office)
    /employment — Set employment type (b2b/uop/uz)
    /salary     — Set minimum salary
    /city       — Set city filter
    /tolerance  — Set how many filters can mismatch
    /latest     — Get recent matching listings
    /stats      — Pipeline statistics

Run with: python -m telegram_bot.bot
"""

import asyncio
import copy
import csv
import datetime
import html
import io
import json
import logging
import os
import threading
import zoneinfo
from pathlib import Path

from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    PreCheckoutQueryHandler,
)
from telegram.ext import (
    filters as tg_filters,
)

from telegram_bot import config_store, payments, reports, serving, tracker
from telegram_bot.analytics import (
    get_analytics_summary,
    is_opted_out,
    log_command,
    log_filter_choice,
    set_opt_out,
)
from telegram_bot.filters import (
    ALL_CATEGORIES,
    ALL_EMPLOYMENT_TYPES,
    ALL_SENIORITIES,
    ALL_WORKPLACES,
    DEFAULT_USER_CONFIG,
    TECH_CATEGORIES,
    filter_listings,
)

logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")


def _parse_admin_chat_id() -> int:
    """Parse the admin chat id from env, tolerating malformed values."""
    raw = os.environ.get("TELEGRAM_CHAT_ID", "0")
    try:
        return int(raw)
    except (TypeError, ValueError):
        logger.warning("TELEGRAM_CHAT_ID is not an integer (%r); admin commands disabled", raw)
        return 0


ADMIN_CHAT_ID = _parse_admin_chat_id()

# --- Daily broadcast timing (08:00 Warsaw, configurable via env) ---
WARSAW_TZ = zoneinfo.ZoneInfo("Europe/Warsaw")
BROADCAST_HOUR = int(os.environ.get("BROADCAST_HOUR", "8"))
BROADCAST_MINUTE = int(os.environ.get("BROADCAST_MINUTE", "0"))

# Serializes read-modify-write cycles on the shared config file within this process.
_config_lock = threading.Lock()

# Coalesce bursts of filter edits into fewer Volume uploads.
PUBLISH_DEBOUNCE_SECONDS = 5.0
_publish_dirty = False
_publish_task: asyncio.Task | None = None

BOT_COMMANDS = [
    BotCommand("start", "Welcome + overview"),
    BotCommand("filters", "View & edit your filters"),
    BotCommand("tolerance", "Set mismatch tolerance"),
    BotCommand("myskills", "Set your skills (ranks results)"),
    BotCommand("latest", "💎 Recent matching listings"),
    BotCommand("premium", "💎 Premium menu (analytics, tracker…)"),
    BotCommand("subscribe", "Premium tiers & pricing"),
    BotCommand("feedback", "Send feedback to the maker"),
    BotCommand("privacy", "Privacy & data info"),
    BotCommand("help", "Show all commands"),
]


def load_config(chat_id: int) -> dict:
    """Load a single user's config, merged over defaults. Never mutates shared state."""
    with _config_lock:
        all_configs = config_store.load_local()
    merged = copy.deepcopy(DEFAULT_USER_CONFIG)
    user_cfg = all_configs.get(str(chat_id))
    if isinstance(user_cfg, dict):
        merged.update(user_cfg)
    return merged


def save_config(chat_id: int, config: dict):
    """Persist a single user's config atomically, then mirror to the Volume."""
    with _config_lock:
        all_configs = config_store.load_local()
        all_configs[str(chat_id)] = config
        config_store.save_local(all_configs)
    _schedule_volume_publish()


def _schedule_volume_publish():
    """Request a (debounced, background) mirror of the local config to the Volume.

    No-op when there's no running event loop (e.g. in tests) — the local file is
    always authoritative, so skipping the mirror is safe.
    """
    global _publish_dirty, _publish_task
    _publish_dirty = True
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    if _publish_task is None or _publish_task.done():
        _publish_task = loop.create_task(_volume_publish_worker())


async def _volume_publish_worker():
    """Upload the local config to the Volume, coalescing rapid successive edits.

    The published copy is enriched with each user's ``max_listings`` cap (derived
    from their subscription tier) so the daily broadcast senders — which run in
    GitHub Actions / Databricks and have no access to payments.db — can give paid
    users a larger batch without querying the subscription store themselves.
    """
    global _publish_dirty
    await asyncio.sleep(PUBLISH_DEBOUNCE_SECONDS)
    while _publish_dirty:
        _publish_dirty = False
        snapshot = config_store.load_local()
        enriched = _enrich_with_caps(snapshot)
        ok = await asyncio.to_thread(config_store.upload_to_volume, enriched)
        if not ok:
            logger.debug("Volume publish skipped/failed; local copy remains authoritative")


def _enrich_with_caps(snapshot: dict) -> dict:
    """Return a copy of the config store with each user's listings cap stamped in.

    Local file stays clean (derived field is only added to the uploaded copy).
    """
    enriched = copy.deepcopy(snapshot)
    for chat_id, cfg in enriched.items():
        if not isinstance(cfg, dict):
            continue
        try:
            cfg["max_listings"] = payments.listing_cap(int(chat_id))
        except (ValueError, TypeError):
            cfg["max_listings"] = payments.FREE_MAX_LISTINGS
    return enriched


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

    # Activate free trial for brand-new users
    if is_new:
        trial_expiry = payments.activate_trial(chat_id)
        if trial_expiry is not None:
            exp_str = datetime.datetime.fromtimestamp(trial_expiry, tz=WARSAW_TZ).strftime(
                "%Y-%m-%d %H:%M"
            )
            text += (
                f"\n\n🎁 <b>Welcome gift!</b> You have 24h of free Premium (Plus tier).\n"
                f"Try /salary Python, /trend, /skills — expires {exp_str} (Warsaw)."
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
        "<b>💎 Premium</b> — open the menu with /premium (or /subscribe):\n"
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
        "Filters &amp; daily alerts are free; 💎 marks premium.</i>"
    )
    await update.message.reply_text(text, parse_mode="HTML")


async def cmd_filters(update: Update, context: ContextTypes.DEFAULT_TYPE):
    log_command(update.effective_chat.id, "filters")
    chat_id = update.effective_chat.id
    args = context.args or []
    if args and args[0].lower() == "clear":
        config = copy.deepcopy(DEFAULT_USER_CONFIG)
        save_config(chat_id, config)
        await update.message.reply_text("🗑️ All filters reset to defaults.")
        return
    config = load_config(chat_id)
    await _send_filters_menu(update.message, config)


def _premium_back_kb() -> InlineKeyboardMarkup:
    """Inline keyboard shown after premium command responses for easy navigation."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("💎 Premium Menu", callback_data="prem:home"),
                InlineKeyboardButton("🎛️ Filters", callback_data="back_filters"),
            ]
        ]
    )


def _build_filters_view(config: dict) -> tuple[str, InlineKeyboardMarkup]:
    """Build the filters overview text + inline keyboard (shared by send/edit)."""
    sections = ["<b>📋 Your Filters</b>\n"]

    tol = config.get("tolerance", 1)
    sections.append(f"⚙️ Tolerance: {tol} mismatch{'es' if tol != 1 else ''} allowed\n")

    sen = config.get("seniorities", [])
    display = " · ".join(ALL_SENIORITIES.get(s, s) for s in sen) if sen else "any"
    sections.append(f"🎯 Seniority: {display}")

    cats = config.get("categories", [])
    display = " · ".join(ALL_CATEGORIES.get(c, c) for c in cats) if cats else "any"
    sections.append(f"📂 Category: {display}")

    techs = config.get("technologies", [])
    display = ", ".join(techs[:6]) + ("…" if len(techs) > 6 else "") if techs else "any"
    sections.append(f"💻 Tech: {display}")

    wp = config.get("workplace_types", [])
    display = " · ".join(ALL_WORKPLACES.get(w, w) for w in wp) if wp else "any"
    sections.append(f"🏠 Workplace: {display}")

    emp = config.get("employment_types", [])
    display = " · ".join(ALL_EMPLOYMENT_TYPES.get(e, e) for e in emp) if emp else "any"
    sections.append(f"📄 Employment: {display}")

    sal = config.get("salary_min", 0)
    sections.append(f"💰 Min salary: {sal} PLN" if sal else "💰 Min salary: any")

    cities = config.get("cities", [])
    display = ", ".join(cities) if cities else "any"
    sections.append(f"🏙️ Cities: {display}")

    skills = config.get("skills", [])
    display = ", ".join(skills[:6]) + ("…" if len(skills) > 6 else "") if skills else "none"
    sections.append(f"🧠 My skills: {display}")

    sections.append("\n<i>Tap a button to edit:</i>")

    keyboard = [
        [
            InlineKeyboardButton("🎯 Seniority", callback_data="menu_seniority"),
            InlineKeyboardButton("📂 Category", callback_data="menu_category"),
        ],
        [
            InlineKeyboardButton("💻 Tech", callback_data="menu_tech"),
            InlineKeyboardButton("🏠 Workplace", callback_data="menu_workplace"),
        ],
        [
            InlineKeyboardButton("📄 Employment", callback_data="menu_employment"),
            InlineKeyboardButton("💰 Salary", callback_data="menu_salary"),
        ],
        [
            InlineKeyboardButton("🏙️ City", callback_data="menu_city"),
            InlineKeyboardButton("⚙️ Tolerance", callback_data="menu_tolerance"),
        ],
        [
            InlineKeyboardButton("🧠 My Skills", callback_data="menu_myskills"),
        ],
    ]
    return "\n".join(sections), InlineKeyboardMarkup(keyboard)


async def _send_filters_menu(message, config: dict):
    """Send the filters overview with an inline keyboard for editing."""
    text, keyboard = _build_filters_view(config)
    await message.reply_text(text, parse_mode="HTML", reply_markup=keyboard)


async def _callback_filters(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle inline keyboard presses from the filters menu."""
    query = update.callback_query
    await query.answer()
    data = query.data
    chat_id = update.effective_chat.id
    config = load_config(chat_id)

    if data == "menu_seniority":
        await _show_toggle_picker(
            query.message, config, "seniorities", ALL_SENIORITIES, "🎯 Seniority", "sen"
        )
    elif data == "menu_category":
        await _show_toggle_picker(
            query.message, config, "categories", ALL_CATEGORIES, "📂 Category", "cat"
        )
    elif data == "menu_workplace":
        await _show_toggle_picker(
            query.message, config, "workplace_types", ALL_WORKPLACES, "🏠 Workplace", "wp"
        )
    elif data == "menu_employment":
        await _show_toggle_picker(
            query.message, config, "employment_types", ALL_EMPLOYMENT_TYPES, "📄 Employment", "emp"
        )
    elif data == "menu_tolerance":
        await _show_tolerance_picker(query.message, config)
    elif data == "menu_tech":
        await query.message.edit_text(
            "💻 <b>Technologies</b>\n\n"
            "Type /tech followed by your choices:\n"
            "<code>/tech Python SQL Docker React</code>\n\n"
            "Browse all: /tech list\nClear: /tech clear",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("◀️ Back", callback_data="back_filters")]]
            ),
        )
    elif data == "menu_salary":
        await query.message.edit_text(
            "💰 <b>Minimum salary</b>\n\n"
            "Type /salary followed by amount:\n"
            "<code>/salary 10000</code>\n\nClear: /salary clear",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("◀️ Back", callback_data="back_filters")]]
            ),
        )
    elif data == "menu_city":
        await query.message.edit_text(
            "🏙️ <b>Cities</b>\n\n"
            "Type /city followed by names:\n"
            "<code>/city Warszawa Kraków Wrocław</code>\n\nClear: /city clear",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("◀️ Back", callback_data="back_filters")]]
            ),
        )
    elif data == "menu_myskills":
        skills = config.get("skills", [])
        current = ", ".join(skills) if skills else "none set"
        await query.message.edit_text(
            f"🧠 <b>My Skills:</b> {current}\n\n"
            "Set: <code>/myskills python sql airflow</code>\n"
            "Clear: <code>/myskills clear</code>\n\n"
            "<i>Ranks your /latest results by % match. (Free)</i>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("◀️ Back", callback_data="back_filters")]]
            ),
        )
    elif data.startswith("toggle_"):
        await _handle_toggle(query, chat_id, config, data)
    elif data.startswith("set_tol_"):
        tol = int(data[8:])
        config["tolerance"] = tol
        save_config(chat_id, config)
        await _show_tolerance_picker(query.message, config, edit=True)
    elif data == "back_filters":
        # Re-render the main filters menu in place
        await _edit_filters_menu(query.message, load_config(chat_id))


async def _edit_filters_menu(message, config: dict):
    """Edit existing message to show filters menu.

    Falls back to sending a new message if the originating message can't be
    edited into text (e.g. a /trend chart, which is a photo message).
    """
    text, keyboard = _build_filters_view(config)
    try:
        await message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
    except Exception:
        await message.reply_text(text, parse_mode="HTML", reply_markup=keyboard)


async def _handle_toggle(query, chat_id: int, config: dict, data: str):
    """Toggle a value and refresh the picker."""
    # Parse: toggle_sen_junior, toggle_cat_python, toggle_wp_remote, toggle_emp_b2b
    parts = data.split("_", 2)  # ['toggle', 'sen', 'junior']
    prefix = parts[1]
    value = parts[2]

    key_map = {
        "sen": "seniorities",
        "cat": "categories",
        "wp": "workplace_types",
        "emp": "employment_types",
    }
    options_map = {
        "sen": ALL_SENIORITIES,
        "cat": ALL_CATEGORIES,
        "wp": ALL_WORKPLACES,
        "emp": ALL_EMPLOYMENT_TYPES,
    }
    label_map = {
        "sen": "🎯 Seniority",
        "cat": "📂 Category",
        "wp": "🏠 Workplace",
        "emp": "📄 Employment",
    }
    dim_map = {"sen": "seniority", "cat": "category", "wp": "workplace", "emp": "employment"}

    key = key_map[prefix]
    current = config.get(key, [])

    if value in current:
        current.remove(value)
        was_added = False
    else:
        current.append(value)
        was_added = True
    config[key] = current
    save_config(chat_id, config)

    # Only log when a filter is enabled (tracks what users want, not what they remove)
    if was_added:
        log_filter_choice(chat_id, dim_map[prefix], [value])

    # Refresh picker in place
    await _show_toggle_picker(
        query.message, config, key, options_map[prefix], label_map[prefix], prefix, edit=True
    )


async def _show_toggle_picker(
    message, config: dict, key: str, options: dict, title: str, prefix: str, edit: bool = False
):
    """Show a toggle picker for any list-based filter.

    Always edits the message in place (invoked from a callback query), so the
    ``edit`` flag is accepted for call-site clarity but does not change behavior.
    """
    current = config.get(key, [])
    keyboard = []

    items = list(options.items())
    # Use 2-column layout for categories (many items), single column for others
    if len(items) > 6:
        for i in range(0, len(items), 2):
            row = []
            for k, label in items[i : i + 2]:
                check = "✅" if k in current else "⬜"
                short = label.split(" ", 1)[1] if " " in label else label
                row.append(
                    InlineKeyboardButton(f"{check} {short}", callback_data=f"toggle_{prefix}_{k}")
                )
            keyboard.append(row)
    else:
        for k, label in items:
            check = "✅" if k in current else "⬜"
            keyboard.append(
                [InlineKeyboardButton(f"{check} {label}", callback_data=f"toggle_{prefix}_{k}")]
            )

    keyboard.append([InlineKeyboardButton("◀️ Back", callback_data="back_filters")])

    text = f"<b>{title}</b> — tap to toggle:\n\n<i>✅ = active, ⬜ = off</i>"
    await message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))


async def _show_tolerance_picker(message, config: dict, edit: bool = False):
    """Show tolerance radio buttons (always edits in place)."""
    current = config.get("tolerance", 1)
    options = [
        (0, "Strict (all must match)"),
        (1, "Flexible (1 mismatch ok)"),
        (2, "Broad (2 mismatches ok)"),
        (3, "Very loose (3 mismatches ok)"),
    ]
    keyboard = []
    for val, label in options:
        check = "🔘" if val == current else "⚪"
        keyboard.append([InlineKeyboardButton(f"{check} {label}", callback_data=f"set_tol_{val}")])
    keyboard.append([InlineKeyboardButton("◀️ Back", callback_data="back_filters")])

    text = "⚙️ <b>Tolerance</b> — how many filters can mismatch:"
    await message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))


async def cmd_seniority(update: Update, context: ContextTypes.DEFAULT_TYPE):
    log_command(update.effective_chat.id, "seniority")
    args = context.args
    valid = set(ALL_SENIORITIES.keys())

    if not args:
        config = load_config(update.effective_chat.id)
        current = config.get("seniorities", [])
        cur_display = " · ".join(ALL_SENIORITIES.get(s, s) for s in current) if current else "any"
        opts = "\n".join(f"  {key} — {label}" for key, label in ALL_SENIORITIES.items())
        await update.message.reply_text(
            f"🎯 <b>Current:</b> {cur_display}\n\n"
            f"<b>Options:</b>\n{opts}\n\n"
            f"<b>Usage:</b> /seniority junior mid",
            parse_mode="HTML",
        )
        return

    levels = [a.lower().strip() for a in args]
    invalid = [lv for lv in levels if lv not in valid]
    if invalid:
        await update.message.reply_text(
            f"❌ Unknown: {', '.join(invalid)}\nValid: {', '.join(sorted(valid))}"
        )
        return

    config = load_config(update.effective_chat.id)
    config["seniorities"] = levels
    save_config(update.effective_chat.id, config)
    log_filter_choice(update.effective_chat.id, "seniority", levels)
    display = " · ".join(ALL_SENIORITIES.get(lv, lv) for lv in levels)
    await update.message.reply_text(f"✅ Seniority → {display}", parse_mode="HTML")


def _parse_tech_args(args: list[str]) -> list[str]:
    """Parse technology arguments, correctly joining multi-word tech names.

    Telegram splits command args by whitespace, so '/tech Apache Airflow Docker'
    becomes ['Apache', 'Airflow', 'Docker']. This function greedily joins tokens
    into known multi-word technology names from ALL_KNOWN_TECHS.

    Strategy: try longest match first (3 words, then 2, then 1). Unknown
    single-word tokens are kept as-is (users can type any tech name).
    """
    from telegram_bot.filters import ALL_KNOWN_TECHS

    # Build a lookup of lowercase multi-word tech names → canonical name
    multi_word_techs: dict[str, str] = {}
    for tech in ALL_KNOWN_TECHS:
        parts = tech.split()
        if len(parts) > 1:
            multi_word_techs[tech.lower()] = tech

    result = []
    i = 0
    while i < len(args):
        matched = False
        # Try longest match first (up to 3 words)
        for length in (3, 2):
            if i + length <= len(args):
                candidate = " ".join(args[i : i + length]).lower()
                if candidate in multi_word_techs:
                    result.append(multi_word_techs[candidate])
                    i += length
                    matched = True
                    break
        if not matched:
            # Single token — use as-is (preserve user casing, or match known)
            token = args[i]
            # Try to match single-word known techs for canonical casing
            known_match = next(
                (t for t in ALL_KNOWN_TECHS if t.lower() == token.lower() and " " not in t), None
            )
            result.append(known_match if known_match else token)
            i += 1
    return result


async def cmd_tech(update: Update, context: ContextTypes.DEFAULT_TYPE):
    log_command(update.effective_chat.id, "tech")
    args = context.args

    if args and args[0].lower() == "list":
        lines = ["<b>💻 Known Technologies:</b>\n"]
        for category, techs in TECH_CATEGORIES.items():
            lines.append(f"{category}")
            lines.append(f"  <code>{', '.join(techs)}</code>\n")
        lines.append("<i>You can use any name — these are just suggestions.</i>")
        await update.message.reply_text("\n".join(lines), parse_mode="HTML")
        return

    if args and args[0].lower() == "clear":
        config = load_config(update.effective_chat.id)
        config["technologies"] = []
        save_config(update.effective_chat.id, config)
        await update.message.reply_text("✅ Technology filter cleared (matching any)")
        return

    if args and args[0].lower() == "add":
        if len(args) < 2:
            await update.message.reply_text("Usage: /tech add Apache Airflow Docker")
            return
        args = args[1:]  # Strip "add", fall through to default additive logic below

    if args and args[0].lower() == "remove":
        if len(args) < 2:
            await update.message.reply_text("Usage: /tech remove Python Docker")
            return
        to_remove = _parse_tech_args(list(args[1:]))
        to_remove_lower = {t.lower() for t in to_remove}
        config = load_config(update.effective_chat.id)
        current = config.get("technologies", [])
        removed = [t for t in current if t.lower() in to_remove_lower]
        config["technologies"] = [t for t in current if t.lower() not in to_remove_lower]
        save_config(update.effective_chat.id, config)
        if removed:
            remaining = ", ".join(config["technologies"]) if config["technologies"] else "any"
            await update.message.reply_text(
                f"✅ Removed: {', '.join(removed)}\n💻 Current: {remaining}"
            )
        else:
            await update.message.reply_text("❌ None of those were in your tech filter.")
        return

    if not args:
        config = load_config(update.effective_chat.id)
        techs = config.get("technologies", [])
        current = ", ".join(techs) if techs else "any (no filter)"
        await update.message.reply_text(
            f"💻 <b>Tech filter:</b> {current}\n\n"
            f"<b>Add:</b> <code>/tech add Python Docker</code>\n"
            f"<b>Remove:</b> <code>/tech remove Python</code>\n"
            f"<b>Browse all:</b> /tech list\n"
            f"<b>Clear:</b> /tech clear\n\n"
            f"<i>🔍 This is a <b>filter</b>: only listings mentioning these "
            f"technologies will appear. See /myskills for ranking without filtering.</i>",
            parse_mode="HTML",
        )
        return

    # Bare args (no add/remove keyword) default to add behavior
    config = load_config(update.effective_chat.id)
    new_techs = _parse_tech_args(args)
    current = config.get("technologies", [])
    current_lower = {t.lower() for t in current}
    added = [t for t in new_techs if t.lower() not in current_lower]
    config["technologies"] = current + added
    save_config(update.effective_chat.id, config)
    log_filter_choice(update.effective_chat.id, "technology", config["technologies"])
    if added:
        await update.message.reply_text(
            f"✅ Added: {', '.join(added)}\n" f"💻 All: {', '.join(config['technologies'])}",
            parse_mode="HTML",
        )
    else:
        await update.message.reply_text("ℹ️ Those are already in your tech filter.")


async def cmd_category(update: Update, context: ContextTypes.DEFAULT_TYPE):
    log_command(update.effective_chat.id, "category")
    args = context.args

    if args and args[0].lower() == "list":
        lines = ["<b>📂 Job Categories (from justjoin.it):</b>\n"]
        for key, label in ALL_CATEGORIES.items():
            lines.append(f"  <code>{key}</code> — {label}")
        await update.message.reply_text("\n".join(lines), parse_mode="HTML")
        return

    if args and args[0].lower() == "clear":
        config = load_config(update.effective_chat.id)
        config["categories"] = []
        save_config(update.effective_chat.id, config)
        await update.message.reply_text("✅ Category filter cleared (matching any)")
        return

    if not args:
        config = load_config(update.effective_chat.id)
        cats = config.get("categories", [])
        current = ", ".join(ALL_CATEGORIES.get(c, c) for c in cats) if cats else "any"
        await update.message.reply_text(
            f"📂 <b>Current:</b> {current}\n\n"
            f"<b>Usage:</b> /category python devops data\n"
            f"<b>Browse:</b> /category list\n"
            f"<b>Clear:</b> /category clear",
            parse_mode="HTML",
        )
        return

    valid = set(ALL_CATEGORIES.keys())
    cats = [a.lower().strip() for a in args]
    invalid = [c for c in cats if c not in valid]
    if invalid:
        await update.message.reply_text(
            f"❌ Unknown: {', '.join(invalid)}\nUse /category list to see valid options."
        )
        return

    config = load_config(update.effective_chat.id)
    config["categories"] = cats
    save_config(update.effective_chat.id, config)
    log_filter_choice(update.effective_chat.id, "category", cats)
    display = ", ".join(ALL_CATEGORIES.get(c, c) for c in cats)
    await update.message.reply_text(f"✅ Categories → {display}", parse_mode="HTML")


async def cmd_workplace(update: Update, context: ContextTypes.DEFAULT_TYPE):
    log_command(update.effective_chat.id, "workplace")
    args = context.args

    if args and args[0].lower() == "clear":
        config = load_config(update.effective_chat.id)
        config["workplace_types"] = []
        save_config(update.effective_chat.id, config)
        await update.message.reply_text("✅ Workplace filter cleared (matching any)")
        return

    if not args:
        config = load_config(update.effective_chat.id)
        wp = config.get("workplace_types", [])
        current = " · ".join(ALL_WORKPLACES.get(w, w) for w in wp) if wp else "any"
        opts = "\n".join(f"  <code>{k}</code> — {v}" for k, v in ALL_WORKPLACES.items())
        await update.message.reply_text(
            f"🏠 <b>Current:</b> {current}\n\n"
            f"<b>Options:</b>\n{opts}\n\n"
            f"<b>Usage:</b> /workplace remote hybrid\n"
            f"<b>Clear:</b> /workplace clear",
            parse_mode="HTML",
        )
        return

    valid = set(ALL_WORKPLACES.keys())
    types = [a.lower().strip() for a in args]
    invalid = [t for t in types if t not in valid]
    if invalid:
        await update.message.reply_text(
            f"❌ Unknown: {', '.join(invalid)}\nValid: {', '.join(valid)}"
        )
        return

    config = load_config(update.effective_chat.id)
    config["workplace_types"] = types
    save_config(update.effective_chat.id, config)
    log_filter_choice(update.effective_chat.id, "workplace", types)
    display = " · ".join(ALL_WORKPLACES.get(t, t) for t in types)
    await update.message.reply_text(f"✅ Workplace → {display}", parse_mode="HTML")


async def cmd_employment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    log_command(update.effective_chat.id, "employment")
    args = context.args

    if args and args[0].lower() == "clear":
        config = load_config(update.effective_chat.id)
        config["employment_types"] = []
        save_config(update.effective_chat.id, config)
        await update.message.reply_text("✅ Employment filter cleared (matching any)")
        return

    if not args:
        config = load_config(update.effective_chat.id)
        emp = config.get("employment_types", [])
        current = " · ".join(ALL_EMPLOYMENT_TYPES.get(e, e) for e in emp) if emp else "any"
        opts = "\n".join(f"  <code>{k}</code> — {v}" for k, v in ALL_EMPLOYMENT_TYPES.items())
        await update.message.reply_text(
            f"📄 <b>Current:</b> {current}\n\n"
            f"<b>Options:</b>\n{opts}\n\n"
            f"<b>Usage:</b> /employment b2b permanent\n"
            f"<b>Clear:</b> /employment clear",
            parse_mode="HTML",
        )
        return

    valid = set(ALL_EMPLOYMENT_TYPES.keys())
    types = [a.lower().strip() for a in args]
    invalid = [t for t in types if t not in valid]
    if invalid:
        await update.message.reply_text(
            f"❌ Unknown: {', '.join(invalid)}\nValid: {', '.join(valid)}"
        )
        return

    config = load_config(update.effective_chat.id)
    config["employment_types"] = types
    save_config(update.effective_chat.id, config)
    log_filter_choice(update.effective_chat.id, "employment", types)
    display = " · ".join(ALL_EMPLOYMENT_TYPES.get(t, t) for t in types)
    await update.message.reply_text(f"✅ Employment → {display}", parse_mode="HTML")


async def cmd_salary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    log_command(update.effective_chat.id, "salary")
    args = context.args

    if args and args[0].lower() == "clear":
        config = load_config(update.effective_chat.id)
        config["salary_min"] = 0
        save_config(update.effective_chat.id, config)
        await update.message.reply_text("✅ Salary filter cleared (matching any)")
        return

    if not args:
        config = load_config(update.effective_chat.id)
        sal = config.get("salary_min", 0)
        current = f"{sal} PLN" if sal else "any"
        await update.message.reply_text(
            f"💰 <b>Current minimum:</b> {current}\n\n"
            f"<b>Set filter:</b> /salary 8000\n"
            f"<i>(monthly PLN — listings paying less won't match)</i>\n"
            f"<b>Clear:</b> /salary clear\n\n"
            f"<b>💎 Salary insights:</b> /salary python\n"
            f"<i>(median + typical range per contract type — premium)</i>",
            parse_mode="HTML",
        )
        return

    try:
        amount = int(args[0].replace(",", "").replace(".", ""))
    except ValueError:
        # Non-numeric arg → premium salary analytics for that technology.
        await _salary_insights(update, args)
        return

    config = load_config(update.effective_chat.id)
    config["salary_min"] = amount
    save_config(update.effective_chat.id, config)
    await update.message.reply_text(f"✅ Min salary → {amount} PLN/month")


async def _salary_insights(update: Update, args: list[str]):
    """Premium: salary stats for a technology (optionally a seniority as 2nd arg)."""
    if not await _require_feature(update, payments.FEATURE_SALARY, "Salary insights"):
        return
    log_command(update.effective_chat.id, "salary_insights")

    tech = args[0]
    seniority = args[1].lower() if len(args) > 1 else None
    stats = await asyncio.to_thread(serving.salary_for_tech, tech, seniority)
    if not stats or not stats.get("groups"):
        await update.message.reply_text(
            f"📭 No salary data for <b>{tech}</b>"
            + (f" ({seniority})" if seniority else "")
            + ".\nTry another technology, e.g. /salary Python",
            parse_mode="HTML",
        )
        return

    lines = [f"💰 <b>Salary — {tech}</b>" + (f" · {seniority}" if seniority else "")]
    lines.append(f"<i>Based on {stats['listing_count']} listings</i>\n")

    # One block per contract basis + currency (largest group first). Salary is
    # period-normalized to monthly upstream, so B2B and UoP are comparable; we
    # still show them separately because they're genuinely different comp
    # (B2B gross vs UoP), not because of a units mismatch.
    for g in stats["groups"]:
        cur = g["currency"]
        head = f"<b>{g['label']}</b> · {cur} · {g['count']} listings"
        lines.append(head)
        if g.get("median") is not None:
            lines.append(f"  📊 Median: <b>{g['median']} {cur}/mo</b>")
        if g.get("p25") is not None and g.get("p75") is not None:
            lines.append(f"  📉 Typical (25–75%): {g['p25']} – {g['p75']} {cur}/mo")
        lines.append("")

    # Per-seniority breakdown (permanent/UoP median), only when not filtered.
    if not seniority:
        by_sen = await asyncio.to_thread(serving.salary_by_seniority, tech)
        rows = [r for r in by_sen if r.get("median")]
        if len(rows) > 1:
            lines.append("\n<b>By seniority (UoP median, PLN):</b>")
            for r in rows:
                label = ALL_SENIORITIES.get((r.get("seniority") or "").lower(), r.get("seniority"))
                lines.append(f"  {label}: {round(r['median'])} ({int(r['n'])})")

    await update.message.reply_text(
        "\n".join(lines), parse_mode="HTML", reply_markup=_premium_back_kb()
    )


async def cmd_city(update: Update, context: ContextTypes.DEFAULT_TYPE):
    log_command(update.effective_chat.id, "city")
    args = context.args

    if args and args[0].lower() == "clear":
        config = load_config(update.effective_chat.id)
        config["cities"] = []
        save_config(update.effective_chat.id, config)
        await update.message.reply_text("✅ City filter cleared (matching any location)")
        return

    if args and args[0].lower() == "remove":
        to_remove = list(args[1:])  # /city remove Kraków — remove specific cities
        if not to_remove:
            await update.message.reply_text("Usage: /city remove Kraków Gdańsk")
            return
        config = load_config(update.effective_chat.id)
        current = config.get("cities", [])
        removed = [c for c in to_remove if c in current]
        config["cities"] = [c for c in current if c not in to_remove]
        save_config(update.effective_chat.id, config)
        if removed:
            remaining = ", ".join(config["cities"]) if config["cities"] else "any"
            await update.message.reply_text(
                f"✅ Removed: {', '.join(removed)}\n🏙️ Current: {remaining}"
            )
        else:
            await update.message.reply_text("❌ None of those were in your filter.")
        return

    if args and args[0].lower() == "list":
        from telegram_bot.filters import KNOWN_CITIES

        cities_sorted = sorted(KNOWN_CITIES)
        await update.message.reply_text(
            "🏙️ <b>Known cities:</b>\n\n"
            f"<code>{', '.join(cities_sorted)}</code>\n\n"
            "<i>You can add any city — these are just the ones we've seen in listings.</i>",
            parse_mode="HTML",
        )
        return

    if not args:
        config = load_config(update.effective_chat.id)
        cities = config.get("cities", [])
        current = ", ".join(cities) if cities else "any (all locations)"
        await update.message.reply_text(
            f"🏙️ <b>Current:</b> {current}\n\n"
            f"<b>Add:</b> /city add Warszawa Kraków\n"
            f"<b>Remove:</b> /city remove Kraków\n"
            f"<b>See known:</b> /city list\n"
            f"<b>Clear all:</b> /city clear\n\n"
            f"<i>Bare /city Warszawa also adds (same as /city add).</i>",
            parse_mode="HTML",
        )
        return

    # Strip optional "add" keyword for consistency with /tech add, /myskills add
    if args and args[0].lower() == "add":
        args = args[1:]
        if not args:
            await update.message.reply_text("Usage: /city add Warszawa Kraków")
            return

    # Additive: add new cities to existing list
    from telegram_bot.filters import KNOWN_CITIES

    config = load_config(update.effective_chat.id)
    current = config.get("cities", [])

    added = []
    warnings = []
    for city in args:
        if city in current:
            continue  # already there
        # Check if known (case-insensitive)
        known_match = next((c for c in KNOWN_CITIES if c.lower() == city.lower()), None)
        if known_match:
            added.append(known_match)  # Use canonical casing
        else:
            # Not in known list — warn but still add
            added.append(city)
            # Suggest close matches
            close = [
                c for c in KNOWN_CITIES if city.lower() in c.lower() or c.lower() in city.lower()
            ]
            if close:
                warnings.append(f"⚠️ '{city}' not recognized. Did you mean: {', '.join(close[:3])}?")
            else:
                warnings.append(f"⚠️ '{city}' not in known cities (added anyway)")

    config["cities"] = current + added
    save_config(update.effective_chat.id, config)
    if added:
        log_filter_choice(update.effective_chat.id, "city", added)

    parts = []
    if added:
        parts.append(f"✅ Added: {', '.join(added)}")
    parts.append(f"🏙️ Current: {', '.join(config['cities'])}")
    if warnings:
        parts.extend(warnings)

    await update.message.reply_text("\n".join(parts))


async def cmd_tolerance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    log_command(update.effective_chat.id, "tolerance")
    args = context.args

    if not args:
        config = load_config(update.effective_chat.id)
        tol = config.get("tolerance", 1)
        await update.message.reply_text(
            f"⚙️ <b>Current tolerance:</b> {tol}\n\n"
            f"<b>What this means:</b>\n"
            f"• 0 = strict — ALL active filters must match\n"
            f"• 1 = one filter can mismatch (recommended)\n"
            f"• 2 = two filters can mismatch (broader results)\n"
            f"• 3+ = very loose matching\n\n"
            f"<b>Example:</b> If you want Python + remote, but tolerance=1,\n"
            f"a listing with Python + hybrid will still appear.\n\n"
            f"<b>⚠️ Note:</b> Seniority is always strict — tolerance never "
            f"forgives a seniority mismatch.\n\n"
            f"<b>Usage:</b> /tolerance 1",
            parse_mode="HTML",
        )
        return

    try:
        tol = int(args[0])
        if tol < 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Enter a number ≥ 0, e.g. /tolerance 1")
        return

    config = load_config(update.effective_chat.id)
    config["tolerance"] = tol
    save_config(update.effective_chat.id, config)

    desc = {0: "strict", 1: "flexible", 2: "broad", 3: "very loose"}.get(tol, "ultra loose")
    await update.message.reply_text(f"✅ Tolerance → {tol} ({desc})")


def is_paid_user(chat_id: int) -> bool:
    """Whether a user has any active paid subscription (admin always counts)."""
    return chat_id == ADMIN_CHAT_ID or payments.is_subscribed(chat_id)


def has_feature(chat_id: int, feature: str) -> bool:
    """Whether a user can use a premium feature (admin bypasses the paywall)."""
    return chat_id == ADMIN_CHAT_ID or payments.has_feature(chat_id, feature)


def _upsell_text(feature_label: str) -> str:
    """Friendly paywall message pointing users to /subscribe."""
    return (
        f"🔒 <b>{feature_label}</b> is a premium feature.\n\n"
        "Unlock it with a subscription — see /subscribe for tiers and prices.\n"
        "<i>Filters and daily alerts stay free for everyone.</i>"
    )


async def _require_feature(update: Update, feature: str, feature_label: str) -> bool:
    """Gate a handler behind a feature. Sends an upsell + returns False if blocked."""
    if has_feature(update.effective_chat.id, feature):
        return True
    await update.message.reply_text(_upsell_text(feature_label), parse_mode="HTML")
    return False


async def _require_feature_chat(context, chat_id: int, feature: str, feature_label: str) -> bool:
    """Feature gate for flows that only have a chat_id (e.g. inline-menu callbacks)."""
    if has_feature(chat_id, feature):
        return True
    await context.bot.send_message(chat_id, _upsell_text(feature_label), parse_mode="HTML")
    return False


async def cmd_latest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Premium: on-demand queries hit the data source directly.
    if not await _require_feature(update, payments.FEATURE_LATEST, "/latest on-demand listings"):
        return
    log_command(update.effective_chat.id, "latest")
    await update.message.reply_text("🔍 Fetching latest listings...")

    config = load_config(update.effective_chat.id)

    cap = payments.listing_cap(update.effective_chat.id)
    try:
        # Databricks / file I/O is blocking — keep it off the event loop.
        listings = await asyncio.to_thread(_get_latest_listings, config, cap)
    except Exception as e:
        logger.error(f"/latest failed: {e}")
        await update.message.reply_text(
            "⚠️ Failed to fetch listings. Data source may be unavailable.\n" "Try again later.",
        )
        return

    if not listings:
        await update.message.reply_text(
            "📭 No recent listings match your filters.\n\n"
            "Try:\n"
            "• Increase /tolerance\n"
            "• Broaden /tech or /category\n"
            "• Check /filters — maybe your criteria are too narrow",
        )
        return

    # Personalization: if the user saved a skill set (/myskills), rank by overlap.
    skills = config.get("skills", [])
    if skills:
        listings = serving.rank_listings_by_skills(listings, skills)

    count = len(listings)
    chat_id = update.effective_chat.id

    # Pro users with the tracker feature get per-listing messages with
    # Applied/Interested/Rejected buttons; everyone else gets the compact,
    # combined (anti-spam) format.
    if has_feature(chat_id, payments.FEATURE_TRACKER):
        for listing in listings[:10]:
            await _send_trackable_listing(update.message, listing)
            await asyncio.sleep(0.2)
    else:
        from telegram_bot.notify import _build_combined_message

        chunks = _build_combined_message(listings[:10])
        for chunk in chunks:
            try:
                await update.message.reply_text(
                    chunk,
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )
            except Exception as e:
                logger.warning(f"Failed to send listing chunk: {e}")
                continue
            await asyncio.sleep(0.3)

    if count > 10:
        await update.message.reply_text(
            f"<i>Showing 10/{count}. Narrow your filters for more relevant results.</i>",
            parse_mode="HTML",
        )


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show pipeline statistics (admin only)."""
    if update.effective_chat.id != ADMIN_CHAT_ID:
        return
    log_command(update.effective_chat.id, "stats")
    stats = await asyncio.to_thread(_get_stats)
    text = (
        "<b>📊 Pipeline Statistics</b>\n\n"
        f"📁 Raw files scraped: {stats.get('raw_files', 'N/A')}\n"
        f"📨 Alerts sent (total): {stats.get('alerts_sent', 'N/A')}\n"
        f"📅 Last scrape: {stats.get('last_scrape', 'N/A')}\n"
    )
    await update.message.reply_text(text, parse_mode="HTML")


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
        f"<b>Your status:</b> {status}\n\n"
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
    """Save feedback to analytics DB."""
    from datetime import datetime, timezone

    from telegram_bot.analytics import _get_conn, _hash_user

    conn = _get_conn()
    now = datetime.now(timezone.utc).isoformat()
    user_hash = _hash_user(chat_id)
    conn.execute(
        "INSERT INTO events (timestamp, user_hash, event_type, event_data) VALUES (?, ?, ?, ?)",
        (now, user_hash, "feedback", text),
    )
    conn.commit()


# --- Data helpers ---


def _get_latest_listings(config: dict, limit: int = payments.FREE_MAX_LISTINGS) -> list[dict]:
    """Try Databricks, fall back to local data files."""
    databricks_host = os.environ.get("DATABRICKS_HOST", "")
    databricks_token = os.environ.get("DATABRICKS_TOKEN", "")
    warehouse_id = os.environ.get("DATABRICKS_WAREHOUSE_ID", "")

    if databricks_host and databricks_token and warehouse_id:
        try:
            return _query_databricks_latest(config, limit)
        except Exception as e:
            logger.warning(f"Databricks failed, using local: {e}")

    return _read_local_latest(config, limit)


def _ondemand_sql_connect():
    """Databricks SQL connection for on-demand user queries, with a SHORT retry.

    The connector defaults to retrying a cold/unreachable warehouse for 900s. For
    interactive commands (/latest, /export, /stats) that would freeze the handler
    for up to 15 min and, if several stack up, exhaust the thread pool and make the
    whole bot unresponsive. We cap it hard so these commands fail fast and fall
    back to local data. Override via ONDEMAND_RETRY_SECONDS.
    """
    from databricks import sql

    host = os.environ.get("DATABRICKS_HOST", "").replace("https://", "").strip().rstrip("/")
    token = os.environ.get("DATABRICKS_TOKEN", "").strip()
    warehouse_id = os.environ.get("DATABRICKS_WAREHOUSE_ID", "").strip()
    retry_seconds = float(os.environ.get("ONDEMAND_RETRY_SECONDS", "25"))
    return sql.connect(
        server_hostname=host,
        http_path=f"/sql/1.0/warehouses/{warehouse_id}",
        access_token=token,
        _retry_stop_after_attempts_count=3,
        _retry_stop_after_attempts_duration=retry_seconds,
        _socket_timeout=retry_seconds,
    )


def _query_databricks_latest(config: dict, limit: int = payments.FREE_MAX_LISTINGS) -> list[dict]:
    """Query Databricks gold mart, apply filter logic in Python."""
    query = """
        SELECT listing_id, title, slug, company_name, seniority,
               employment_type, workplace_type, category,
               salary_min, salary_max, currency,
               posted_date, technologies, cities
        FROM job_market.gold.mart_market_snapshot
        ORDER BY posted_date DESC
        LIMIT 500
    """

    with _ondemand_sql_connect() as conn:
        with conn.cursor() as cursor:
            cursor.execute(query)
            columns = [desc[0] for desc in cursor.description]
            rows = [
                {k: (v.tolist() if hasattr(v, "tolist") else v) for k, v in zip(columns, row)}
                for row in cursor.fetchall()
            ]

    # Apply tolerance-based filter
    return filter_listings(rows, config)[:limit]


def _read_local_latest(config: dict, limit: int = payments.FREE_MAX_LISTINGS) -> list[dict]:
    """Read most recent local data file and filter."""
    data_dir = Path(__file__).parent.parent / "data"
    if not data_dir.exists():
        return []

    files = sorted(data_dir.glob("raw_listings_*.json"), reverse=True)
    if not files:
        return []

    with open(files[0]) as f:
        data = json.load(f)

    listings = data.get("listings", [])
    return filter_listings(listings[:200], config)[:limit]


def _get_stats() -> dict:
    stats = {}

    data_dir = Path(__file__).parent.parent / "data"
    if data_dir.exists():
        files = list(data_dir.glob("raw_listings_*.json"))
        stats["raw_files"] = len(files)
        if files:
            latest = sorted(files, reverse=True)[0]
            stem = latest.stem
            parts = stem.replace("raw_listings_", "")
            stats["last_scrape"] = parts.replace("_", " @ ")
    else:
        stats["raw_files"] = 0
        stats["last_scrape"] = "No data yet"

    try:
        host = os.environ.get("DATABRICKS_HOST", "").replace("https://", "")
        warehouse_id = os.environ.get("DATABRICKS_WAREHOUSE_ID", "")
        token = os.environ.get("DATABRICKS_TOKEN", "")
        if host and warehouse_id and token:
            with _ondemand_sql_connect() as conn:
                with conn.cursor() as cursor:
                    cursor.execute("SELECT COUNT(*) FROM job_market.gold.telegram_alerts_sent")
                    stats["alerts_sent"] = cursor.fetchone()[0]
        else:
            stats["alerts_sent"] = "N/A (no DB)"
    except Exception:
        stats["alerts_sent"] = "N/A (DB unavailable)"

    return stats


# --- Premium commands ---

# Small in-memory cache of recently shown listings so tracker inline buttons can
# recover a listing's title/company/url from just its id (kept out of callback
# data, which is limited to 64 bytes). Lost on restart — harmless, since the
# tracker row already persists the metadata captured at button-press time.
_recent_listings: dict[str, dict] = {}
_RECENT_MAX = 2000


def _esc(value: object) -> str:
    return html.escape(str(value))


def _remember_listing(listing: dict) -> str:
    """Cache a listing's display metadata keyed by its id. Returns the id."""
    lid = str(listing.get("listing_id", "") or "")
    if not lid:
        return lid
    slug = listing.get("slug", "")
    url = f"https://justjoin.it/offers/{slug}" if slug else ""
    _recent_listings[lid] = {
        "title": listing.get("title"),
        "company": listing.get("company_name"),
        "url": url,
    }
    if len(_recent_listings) > _RECENT_MAX:
        for k in list(_recent_listings)[: _RECENT_MAX // 2]:
            _recent_listings.pop(k, None)
    return lid


async def _send_trackable_listing(message, listing: dict):
    """Send one listing with Applied/Interested/Rejected inline buttons."""
    from telegram_bot.notify import format_listing

    lid = _remember_listing(listing)
    text = format_listing(listing)
    pct = listing.get("match_pct")
    if pct is not None:
        text = f"🎯 <b>{pct}% skill match</b>\n{text}"

    markup = None
    if lid:
        markup = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("✅ Applied", callback_data=f"trk:applied:{lid}"),
                    InlineKeyboardButton("👀 Interested", callback_data=f"trk:interested:{lid}"),
                    InlineKeyboardButton("❌ Rejected", callback_data=f"trk:rejected:{lid}"),
                ]
            ]
        )
    await message.reply_text(
        text, parse_mode="HTML", disable_web_page_preview=True, reply_markup=markup
    )


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
            "🏢 <b>Company intel</b>\n\nUsage: <code>/company Allegro</code>",
            parse_mode="HTML",
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


async def cmd_myskills(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Free personalization: save your skill set; /latest is ranked by % match."""
    log_command(update.effective_chat.id, "myskills")
    chat_id = update.effective_chat.id
    config = load_config(chat_id)
    args = context.args

    if not args:
        skills = config.get("skills", [])
        current = ", ".join(skills) if skills else "none set"
        await update.message.reply_text(
            f"🧠 <b>Your skills:</b> {_esc(current)}\n\n"
            "Add: <code>/myskills add Python Apache Airflow</code>\n"
            "Remove: <code>/myskills remove SQL</code>\n"
            "Browse: <code>/myskills list</code>\n"
            "Clear all: <code>/myskills clear</code>\n\n"
            "<i>🏆 This <b>ranks</b> your /latest results by % skill overlap — "
            "it does NOT exclude listings. Use /tech to filter instead.</i>",
            parse_mode="HTML",
        )
        return

    if args[0].lower() == "clear":
        config["skills"] = []
        save_config(chat_id, config)
        await update.message.reply_text("✅ Skills cleared")
        return

    if args[0].lower() == "list":
        lines = ["<b>🧠 Skill suggestions (same as /tech list):</b>\n"]
        for category, techs in TECH_CATEGORIES.items():
            lines.append(f"{category}")
            lines.append(f"  <code>{', '.join(techs)}</code>\n")
        lines.append("<i>You can use any name — these are just suggestions.</i>")
        await update.message.reply_text("\n".join(lines), parse_mode="HTML")
        return

    if args[0].lower() == "add":
        if len(args) < 2:
            await update.message.reply_text("Usage: /myskills add Python Docker")
            return
        args = args[1:]  # Strip "add", fall through to default additive logic below

    if args[0].lower() == "remove":
        if len(args) < 2:
            await update.message.reply_text("Usage: /myskills remove Python Docker")
            return
        to_remove = _parse_tech_args(list(args[1:]))
        to_remove_lower = {s.lower() for s in to_remove}
        current = config.get("skills", [])
        removed = [s for s in current if s.lower() in to_remove_lower]
        config["skills"] = [s for s in current if s.lower() not in to_remove_lower]
        save_config(chat_id, config)
        if removed:
            remaining = ", ".join(config["skills"]) if config["skills"] else "none"
            await update.message.reply_text(
                f"✅ Removed: {_esc(', '.join(removed))}\n🧠 Current: {_esc(remaining)}",
                parse_mode="HTML",
            )
        else:
            await update.message.reply_text("❌ None of those were in your skills.")
        return

    # Default: additive (no overwrite)
    new_skills = _parse_tech_args(args)
    current = config.get("skills", [])
    current_lower = {s.lower() for s in current}
    added = [s for s in new_skills if s.lower() not in current_lower]
    config["skills"] = current + added
    save_config(chat_id, config)
    if added:
        await update.message.reply_text(
            f"✅ Added: {_esc(', '.join(added))}\n"
            f"🧠 All skills: {_esc(', '.join(config['skills']))}",
            parse_mode="HTML",
        )
    else:
        await update.message.reply_text("ℹ️ Those skills are already in your list.")


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


# --- Application tracker commands ---


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
    meta = _recent_listings.get(lid, {})
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
    await _do_mytracker(context, update.effective_chat.id)


async def _do_mytracker(context, chat_id: int):
    """Show the user's tracked applications."""
    if not await _require_feature_chat(
        context, chat_id, payments.FEATURE_TRACKER, "Application tracker"
    ):
        return
    log_command(chat_id, "mytracker")
    c = await asyncio.to_thread(tracker.counts, chat_id)
    apps = await asyncio.to_thread(tracker.list_applications, chat_id)
    if not apps:
        await context.bot.send_message(
            chat_id,
            "📋 Your tracker is empty.\n"
            "Tap the buttons under /latest listings, or use /applied &lt;id&gt;.",
            parse_mode="HTML",
        )
        return

    icon = {"applied": "✅", "interested": "👀", "rejected": "❌"}
    header = (
        f"📋 <b>Your tracker</b> — "
        f"✅ {c.get('applied', 0)} · 👀 {c.get('interested', 0)} · ❌ {c.get('rejected', 0)}\n"
    )
    lines = [header]
    for a in apps[:40]:
        title = a.get("title") or a.get("listing_id")
        company = f" @ {_esc(a['company'])}" if a.get("company") else ""
        link = f"\n   {a['url']}" if a.get("url") else ""
        lines.append(f"{icon.get(a['status'], '•')} {_esc(title)}{company}{link}")
    await context.bot.send_message(
        chat_id, "\n".join(lines), parse_mode="HTML", disable_web_page_preview=True
    )


# --- Subscriptions (Telegram Stars) ---


async def cmd_subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    log_command(update.effective_chat.id, "subscribe")
    await _show_subscribe_menu(context, update.effective_chat.id)


async def _show_subscribe_menu(context, chat_id: int):
    """Show tiers and let the user buy one with Telegram Stars."""
    sub = payments.get_subscription(chat_id)

    lines = ["💎 <b>Premium tiers</b>\n"]
    if sub:
        exp = datetime.datetime.fromtimestamp(sub["expires_at"]).strftime("%Y-%m-%d")
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
    payments.record_payment(sp.telegram_payment_charge_id, chat_id, tier, sp.total_amount)
    expires = payments.activate(chat_id, tier)
    logger.info(
        "Payment ok: chat=%s tier=%s stars=%s charge_id=%s",
        chat_id,
        tier,
        sp.total_amount,
        sp.telegram_payment_charge_id,
    )
    # Republish config so the broadcast senders pick up this user's new cap.
    _schedule_volume_publish()
    exp = datetime.datetime.fromtimestamp(expires).strftime("%Y-%m-%d")
    await update.message.reply_text(
        f"🎉 <b>{payments.TIERS[tier]['name']}</b> is active until {exp}!\n\n"
        "Try /report, /skills Python, /salary Python, or /latest.",
        parse_mode="HTML",
    )


# --- Admin: refresh the serving cache ---


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
    # Republish config so the broadcast senders drop this user back to the free cap.
    _schedule_volume_publish()
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


async def cmd_refresh(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin: force a resync of the gold marts into the local serving cache."""
    if update.effective_chat.id != ADMIN_CHAT_ID:
        return
    log_command(update.effective_chat.id, "refresh")
    await update.message.reply_text("🔄 Syncing marts from Databricks…")
    synced = await asyncio.to_thread(serving.sync_marts)
    if synced:
        body = "\n".join(f"• {t}: {n} rows" for t, n in synced.items())
        await update.message.reply_text(f"✅ Serving cache refreshed:\n{body}")
    else:
        await update.message.reply_text(
            "⚠️ Sync returned nothing. Check DATABRICKS_* env vars, that duckdb is "
            "installed, and that Databricks is reachable."
        )


# --- Callback routers for premium inline buttons ---


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

    meta = _recent_listings.get(lid, {})
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
        status = f"✅ {payments.TIERS[sub['tier']]['name']} active"
    else:
        status = "🆓 Free tier — tap Subscribe to unlock 💎"

    text = (
        "💎 <b>Premium menu</b>\n"
        f"<i>{status}</i>\n\n"
        "Analytics &amp; tools built on the live market data. Tap an item — the ones "
        "that need a keyword will show you how.\n\n"
        "<i>Filters &amp; daily alerts stay free for everyone.</i>"
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
                InlineKeyboardButton("💎 Subscribe", callback_data="prem:subscribe"),
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


# --- Daily broadcast job (08:00 Warsaw) ---


async def _daily_broadcast_job(context: ContextTypes.DEFAULT_TYPE):
    """Send daily matching listings to all users at 08:00 Warsaw.

    This decouples the broadcast from pipeline completion — users always get
    their digest at a predictable time regardless of scrape/Databricks timing.
    """
    logger.info("Daily broadcast triggered (08:00 Warsaw)")
    try:
        from telegram_bot.notify import run_daily_broadcast

        total = await asyncio.to_thread(run_daily_broadcast)
        logger.info(f"Daily broadcast complete: {total} listings sent")
    except Exception as e:
        logger.error(f"Daily broadcast failed: {e}")


# --- Bot setup ---


async def post_init(application: Application):
    """Register command menu with Telegram and seed config from the Volume."""
    await application.bot.set_my_commands(BOT_COMMANDS)

    # On a fresh host with no local config, pull the last-published copy from the
    # Volume so users don't lose their filters when the bot moves machines.
    if not config_store.LOCAL_PATH.exists() and config_store.volume_enabled():
        remote = await asyncio.to_thread(config_store.download_from_volume)
        if remote:
            config_store.save_local(remote)
            logger.info("Seeded local user config from Volume (%d users)", len(remote))

    logger.info("Bot command menu registered")

    # Keep the premium serving cache warm using the JobQueue: an initial sync on
    # startup (only if the cache is stale) and a periodic refresh thereafter.
    # sync_marts runs in a worker thread so it never blocks the event loop, and
    # premium queries always hit the fast local DuckDB cache instead of a cold
    # Databricks warehouse.
    jq = application.job_queue
    if jq is not None:
        jq.run_once(_startup_sync_job, when=1)
        jq.run_repeating(
            _periodic_sync_job,
            interval=SERVING_SYNC_INTERVAL_SECONDS,
            first=SERVING_SYNC_INTERVAL_SECONDS,
        )
        # Fixed daily broadcast at 08:00 Warsaw (decoupled from pipeline).
        jq.run_daily(
            _daily_broadcast_job,
            time=datetime.time(hour=BROADCAST_HOUR, minute=BROADCAST_MINUTE, tzinfo=WARSAW_TZ),
            name="daily_broadcast",
        )
        # Renewal reminders: nudge users whose subscription is expiring / in grace.
        jq.run_repeating(
            _renewal_reminder_job,
            interval=RENEWAL_CHECK_INTERVAL_SECONDS,
            first=300,
        )
    else:
        logger.warning(
            "JobQueue unavailable — serving cache won't auto-refresh. "
            "Install python-telegram-bot[job-queue], or use the admin /refresh command."
        )


# How often to refresh the serving cache in the background.
SERVING_SYNC_INTERVAL_SECONDS = int(os.environ.get("SERVING_SYNC_INTERVAL_SECONDS", str(6 * 3600)))


async def _startup_sync_job(context: ContextTypes.DEFAULT_TYPE):
    """Sync the serving cache on startup, but only if it's missing/stale."""
    try:
        if serving.is_stale():
            logger.info("Serving cache stale/missing — syncing marts on startup…")
            synced = await asyncio.to_thread(serving.sync_marts)
            logger.info("Startup mart sync: %s", synced or "nothing synced")
    except Exception as e:
        logger.warning("Startup mart sync failed: %s", e)


async def _periodic_sync_job(context: ContextTypes.DEFAULT_TYPE):
    """Refresh the serving cache on the background interval."""
    try:
        synced = await asyncio.to_thread(serving.sync_marts)
        logger.info("Periodic mart sync: %s", synced or "nothing synced")
    except Exception as e:
        logger.warning("Periodic mart sync failed: %s", e)


# How often to check for subscriptions needing a renewal nudge.
RENEWAL_CHECK_INTERVAL_SECONDS = int(
    os.environ.get("RENEWAL_CHECK_INTERVAL_SECONDS", str(6 * 3600))
)


async def _renewal_reminder_job(context: ContextTypes.DEFAULT_TYPE):
    """Nudge users whose subscription is expiring soon or in its grace window."""
    try:
        due = await asyncio.to_thread(payments.due_for_reminder)
    except Exception as e:
        logger.warning("Renewal reminder scan failed: %s", e)
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
    app.add_handler(CommandHandler("refresh", cmd_refresh))
    app.add_handler(CommandHandler("refund", cmd_refund))

    # Patterned callback routers MUST be registered before the catch-all filters
    # handler so tracker/subscribe/premium taps aren't swallowed by _callback_filters.
    app.add_handler(CallbackQueryHandler(_callback_tracker, pattern="^trk:"))
    app.add_handler(CallbackQueryHandler(_callback_subscribe, pattern="^subtier:"))
    app.add_handler(CallbackQueryHandler(_callback_premium, pattern="^prem:"))
    app.add_handler(CallbackQueryHandler(_callback_filters))

    logger.info("Bot starting (long-polling)...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
