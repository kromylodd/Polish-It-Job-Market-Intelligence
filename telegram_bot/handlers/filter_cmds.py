"""Filter commands and the interactive /filters menu.

Covers the filter overview + inline editor callbacks and every filter-setting
command (/seniority, /tech, /category, /workplace, /employment, /salary, /city,
/tolerance) plus the /myskills alias. ``/salary <tech>`` falls through to the
premium salary-insights view kept here alongside the command it extends.
"""

from __future__ import annotations

import asyncio
import copy

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from telegram_bot import payments, serving
from telegram_bot.analytics import log_command, log_filter_choice
from telegram_bot.filters import (
    ALL_CATEGORIES,
    ALL_EMPLOYMENT_TYPES,
    ALL_SENIORITIES,
    ALL_WORKPLACES,
    DEFAULT_USER_CONFIG,
    TECH_CATEGORIES,
)
from telegram_bot.handlers.common import (
    _premium_back_kb,
    _require_feature,
    load_config,
    save_config,
)


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
        config["skills"] = []
        save_config(update.effective_chat.id, config)
        await update.message.reply_text("✅ Tech/skills cleared (matching any, no ranking)")
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
        # Keep skills in sync
        config["skills"] = config["technologies"][:]
        save_config(update.effective_chat.id, config)
        if removed:
            remaining = ", ".join(config["technologies"]) if config["technologies"] else "any"
            await update.message.reply_text(
                f"✅ Removed: {', '.join(removed)}\n💻 Current: {remaining}"
            )
        else:
            await update.message.reply_text("❌ None of those were in your tech list.")
        return

    if not args:
        config = load_config(update.effective_chat.id)
        techs = config.get("technologies", [])
        current = ", ".join(techs) if techs else "any (no filter)"
        await update.message.reply_text(
            f"💻 <b>Your tech/skills:</b> {current}\n\n"
            f"<b>Add:</b> <code>/tech add Python Docker</code>\n"
            f"<b>Remove:</b> <code>/tech remove Python</code>\n"
            f"<b>Browse all:</b> /tech list\n"
            f"<b>Clear:</b> /tech clear\n\n"
            f"<i>Your techs are used for both <b>filtering</b> (only show listings "
            f"with these techs) and <b>ranking</b> (% skill match in results).</i>",
            parse_mode="HTML",
        )
        return

    # Bare args (no add/remove keyword) default to add behavior
    config = load_config(update.effective_chat.id)
    new_techs = _parse_tech_args(args)
    # Deduplicate within the same message
    seen: set[str] = set()
    unique_new: list[str] = []
    for t in new_techs:
        if t.lower() not in seen:
            seen.add(t.lower())
            unique_new.append(t)
    current = config.get("technologies", [])
    current_lower = {t.lower() for t in current}
    added = [t for t in unique_new if t.lower() not in current_lower]
    config["technologies"] = current + added
    # Keep skills in sync — same list
    config["skills"] = config["technologies"][:]
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


async def cmd_myskills(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Alias for /tech — kept for backward compatibility."""
    await cmd_tech(update, context)
