"""
Interactive Telegram bot for Polish IT Job Market Intelligence.

Commands:
    /start   — Welcome message + quick summary
    /help    — List available commands
    /filters — Show current alert filter settings
    /seniority <levels> — Set seniority filter (e.g. /seniority junior mid)
    /tech <technologies> — Set technology filter (e.g. /tech Python SQL dbt)
    /latest  — Fetch most recent matching listings on demand
    /stats   — Pipeline stats (listings collected, alerts sent)

Run with: python -m telegram_bot.bot
"""

import json
import logging
import os
from pathlib import Path

from telegram import BotCommand, Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")

# User preferences stored in a local JSON file.
# For a single-user bot this is sufficient; for multi-user, move to a DB.
CONFIG_PATH = Path(__file__).parent / "user_config.json"

# Expanded default technology filters grouped by category
TECH_CATEGORIES = {
    "🐍 Languages": ["Python", "SQL", "Java", "Scala", "Go", "TypeScript"],
    "📊 Data & Analytics": [
        "Apache Spark",
        "Apache Kafka",
        "Apache Airflow",
        "dbt",
        "Pandas",
        "PySpark",
    ],
    "☁️ Cloud & Infra": [
        "AWS",
        "Azure",
        "GCP",
        "Docker",
        "Kubernetes",
        "Terraform",
    ],
    "🗄️ Databases": [
        "PostgreSQL",
        "MongoDB",
        "Redis",
        "Elasticsearch",
        "Snowflake",
        "Databricks",
    ],
    "🤖 ML & AI": [
        "TensorFlow",
        "PyTorch",
        "MLflow",
        "scikit-learn",
        "LLM",
        "OpenAI",
    ],
}

# Flat list of all known techs for validation hints
ALL_KNOWN_TECHS = [tech for techs in TECH_CATEGORIES.values() for tech in techs]

DEFAULT_CONFIG = {
    "seniorities": ["junior", "mid"],
    "technologies": [
        "Python",
        "SQL",
        "Apache Spark",
        "dbt",
        "Apache Airflow",
        "Docker",
        "AWS",
        "PostgreSQL",
        "Pandas",
        "Kafka",
    ],
}

# Command menu shown next to the send button
BOT_COMMANDS = [
    BotCommand("start", "Welcome + overview"),
    BotCommand("filters", "View current alert filters"),
    BotCommand("seniority", "Set seniority filter"),
    BotCommand("tech", "Set technology filter"),
    BotCommand("latest", "Get recent matching listings"),
    BotCommand("stats", "Pipeline statistics"),
    BotCommand("help", "Show all commands"),
]


def load_config() -> dict:
    """Load user config from disk."""
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH) as f:
            return json.load(f)
    return DEFAULT_CONFIG.copy()


def save_config(config: dict):
    """Persist user config to disk."""
    with open(CONFIG_PATH, "w") as f:
        json.dump(config, f, indent=2)


# --- Command handlers ---


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command."""
    text = (
        "👋 <b>Polish IT Job Market Intelligence</b>\n\n"
        "I send you daily alerts for IT job listings matching your filters "
        "(seniority, technologies) from justjoin.it.\n\n"
        "<b>Commands:</b>\n"
        "📋 /filters — view your current alert settings\n"
        "🎯 /seniority — change seniority filter\n"
        "💻 /tech — change technology filter\n"
        "🔍 /latest — get recent matching listings now\n"
        "📊 /stats — pipeline statistics\n"
        "❓ /help — show all commands"
    )
    await update.message.reply_text(text, parse_mode="HTML")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /help command."""
    text = (
        "<b>Available commands:</b>\n\n"
        "📋 /filters — show current filter settings\n\n"
        "🎯 /seniority &lt;levels&gt; — set seniority filter\n"
        "   <i>e.g. /seniority junior mid senior</i>\n\n"
        "💻 /tech &lt;technologies&gt; — set technology filter\n"
        "   <i>e.g. /tech Python SQL dbt Airflow</i>\n"
        "   <i>Use /tech list to see all known technologies</i>\n\n"
        "🔍 /latest — fetch most recent matching listings\n\n"
        "📊 /stats — pipeline &amp; alert statistics\n\n"
        "Filters affect both daily alerts and /latest results."
    )
    await update.message.reply_text(text, parse_mode="HTML")


async def cmd_filters(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /filters command — show current settings with nice grouping."""
    config = load_config()
    seniorities = config.get("seniorities", [])
    technologies = config.get("technologies", [])

    # Format seniorities with emoji
    sen_display = " · ".join(s.capitalize() for s in seniorities) if seniorities else "Any"

    # Group technologies by category for display
    tech_lines = []
    matched_techs = set()
    for category, techs in TECH_CATEGORIES.items():
        active = [t for t in techs if t in technologies]
        if active:
            tech_lines.append(f"  {category}: {', '.join(active)}")
            matched_techs.update(active)

    # Any techs not in known categories
    uncategorized = [t for t in technologies if t not in matched_techs]
    if uncategorized:
        tech_lines.append(f"  🔧 Other: {', '.join(uncategorized)}")

    tech_display = "\n".join(tech_lines) if tech_lines else "  Any"

    text = (
        "<b>📋 Current Alert Filters</b>\n\n"
        f"<b>🎯 Seniority:</b> {sen_display}\n\n"
        f"<b>💻 Technologies:</b>\n{tech_display}\n\n"
        "<i>Change with /seniority or /tech</i>"
    )
    await update.message.reply_text(text, parse_mode="HTML")


async def cmd_seniority(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /seniority command — set seniority filter."""
    args = context.args
    valid_levels = {"junior", "mid", "senior", "lead", "manager"}

    if not args:
        config = load_config()
        current = " · ".join(s.capitalize() for s in config.get("seniorities", []))
        levels_display = " | ".join(sorted(valid_levels))
        await update.message.reply_text(
            f"<b>🎯 Current seniority filter:</b> {current or 'Any'}\n\n"
            f"<b>Usage:</b> /seniority junior mid senior\n"
            f"<b>Valid levels:</b> {levels_display}",
            parse_mode="HTML",
        )
        return

    # Normalize and validate
    levels = [a.lower().strip() for a in args]
    invalid = [lv for lv in levels if lv not in valid_levels]
    if invalid:
        await update.message.reply_text(
            f"❌ Unknown level(s): {', '.join(invalid)}\n"
            f"Valid: {', '.join(sorted(valid_levels))}",
        )
        return

    config = load_config()
    config["seniorities"] = levels
    save_config(config)
    display = " · ".join(lv.capitalize() for lv in levels)
    await update.message.reply_text(
        f"✅ Seniority filter updated:\n🎯 {display}",
        parse_mode="HTML",
    )


async def cmd_tech(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /tech command — set technology filter."""
    args = context.args

    # /tech list — show all known technologies by category
    if args and args[0].lower() == "list":
        lines = ["<b>💻 Known Technologies:</b>\n"]
        for category, techs in TECH_CATEGORIES.items():
            lines.append(f"{category}")
            lines.append(f"  <code>{', '.join(techs)}</code>\n")
        lines.append("<i>You can use any name — these are just suggestions.</i>")
        await update.message.reply_text("\n".join(lines), parse_mode="HTML")
        return

    if not args:
        config = load_config()
        technologies = config.get("technologies", [])
        current = ", ".join(technologies) if technologies else "Any"
        await update.message.reply_text(
            f"<b>💻 Current technology filter:</b>\n{current}\n\n"
            f"<b>Usage:</b> /tech Python SQL dbt Airflow\n"
            f"<b>Browse available:</b> /tech list",
            parse_mode="HTML",
        )
        return

    # Accept as-is (case-sensitive, tech names are proper nouns)
    config = load_config()
    config["technologies"] = list(args)
    save_config(config)
    await update.message.reply_text(
        f"✅ Technology filter updated:\n💻 {', '.join(args)}",
        parse_mode="HTML",
    )


async def cmd_latest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /latest — fetch recent listings from local data or Databricks."""
    await update.message.reply_text("🔍 Fetching latest listings...")

    config = load_config()

    try:
        listings = _get_latest_listings(config)
    except Exception as e:
        logger.error(f"/latest failed: {e}")
        await update.message.reply_text(
            "⚠️ Failed to fetch listings. The data source may be unavailable.\n"
            "Try again later or check /stats for pipeline status.",
        )
        return

    if not listings:
        await update.message.reply_text(
            "📭 No recent listings match your current filters.\n\n"
            "This could mean:\n"
            "• No scrape has run yet (check /stats)\n"
            "• Your filters are too narrow — try /tech list or /seniority\n"
            "• No new matching jobs in the last few days",
        )
        return

    count = len(listings)
    header = f"📋 <b>{count} recent match{'es' if count != 1 else ''}:</b>"
    await update.message.reply_text(header, parse_mode="HTML")

    import asyncio

    from telegram_bot.notify import format_listing

    for listing in listings[:10]:
        try:
            await update.message.reply_text(
                format_listing(listing),
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        except Exception as e:
            logger.warning(f"Failed to format/send listing: {e}")
            continue
        await asyncio.sleep(0.3)


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /stats — show pipeline statistics."""
    stats = _get_stats()
    text = (
        "<b>📊 Pipeline Statistics</b>\n\n"
        f"📁 Raw files scraped: {stats.get('raw_files', 'N/A')}\n"
        f"📨 Alerts sent (total): {stats.get('alerts_sent', 'N/A')}\n"
        f"📅 Last scrape: {stats.get('last_scrape', 'N/A')}\n"
    )
    await update.message.reply_text(text, parse_mode="HTML")


# --- Data helpers ---


def _get_latest_listings(config: dict) -> list[dict]:
    """Try to get listings from Databricks, fall back to local data files."""
    # Try Databricks first
    databricks_host = os.environ.get("DATABRICKS_HOST", "")
    databricks_token = os.environ.get("DATABRICKS_TOKEN", "")
    warehouse_id = os.environ.get("DATABRICKS_WAREHOUSE_ID", "")

    if databricks_host and databricks_token and warehouse_id:
        try:
            return _query_databricks_latest(config)
        except Exception as e:
            logger.warning(f"Databricks query failed, falling back to local: {e}")

    # Fallback: read from local data/ files
    return _read_local_latest(config)


def _query_databricks_latest(config: dict) -> list[dict]:
    """Query Databricks gold mart for recent matching listings."""
    from databricks import sql

    host = os.environ["DATABRICKS_HOST"].replace("https://", "")
    warehouse_id = os.environ["DATABRICKS_WAREHOUSE_ID"]
    token = os.environ["DATABRICKS_TOKEN"]

    seniorities = config.get("seniorities", [])
    technologies = config.get("technologies", [])

    # Build dynamic WHERE clause
    where_parts = ["posted_date >= CURRENT_DATE - INTERVAL 3 DAYS"]
    if seniorities:
        sen_list = ", ".join(f"'{s}'" for s in seniorities)
        where_parts.append(f"seniority IN ({sen_list})")

    where_clause = " AND ".join(where_parts)

    query = f"""
        SELECT listing_id, title, slug, company_name, seniority,
               employment_type, workplace_type, category,
               salary_min, salary_max, currency,
               posted_date, technologies, cities
        FROM job_market.gold.mart_junior_market_snapshot
        WHERE {where_clause}
        ORDER BY posted_date DESC
        LIMIT 20
    """

    with sql.connect(
        server_hostname=host,
        http_path=f"/sql/1.0/warehouses/{warehouse_id}",
        access_token=token,
    ) as conn:
        with conn.cursor() as cursor:
            cursor.execute(query)
            columns = [desc[0] for desc in cursor.description]
            rows = [dict(zip(columns, row)) for row in cursor.fetchall()]

    # Filter by technologies in Python (array contains is tricky in SQL)
    if technologies:
        rows = [
            r
            for r in rows
            if isinstance(r.get("technologies"), list)
            and any(t in r["technologies"] for t in technologies)
        ]

    return rows


def _read_local_latest(config: dict) -> list[dict]:
    """Read the most recent local data file and filter."""
    data_dir = Path(__file__).parent.parent / "data"
    if not data_dir.exists():
        return []

    files = sorted(data_dir.glob("raw_listings_*.json"), reverse=True)
    if not files:
        return []

    with open(files[0]) as f:
        data = json.load(f)

    listings = data.get("listings", [])
    technologies = config.get("technologies", [])
    seniorities = config.get("seniorities", [])

    # Filter
    results = []
    for listing in listings[:100]:
        seniority = listing.get("seniority", "").lower()
        if seniorities and seniority not in seniorities:
            continue
        techs = listing.get("technologies", [])
        if isinstance(techs, list) and technologies:
            if not any(t in techs for t in technologies):
                continue
        results.append(listing)
        if len(results) >= 20:
            break

    return results


def _get_stats() -> dict:
    """Gather basic pipeline stats."""
    stats = {}

    # Count local data files
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

    # Try to count alerts from Databricks
    try:
        from databricks import sql

        host = os.environ.get("DATABRICKS_HOST", "").replace("https://", "")
        warehouse_id = os.environ.get("DATABRICKS_WAREHOUSE_ID", "")
        token = os.environ.get("DATABRICKS_TOKEN", "")
        if host and warehouse_id and token:
            with sql.connect(
                server_hostname=host,
                http_path=f"/sql/1.0/warehouses/{warehouse_id}",
                access_token=token,
            ) as conn:
                with conn.cursor() as cursor:
                    cursor.execute("SELECT COUNT(*) FROM job_market.gold.telegram_alerts_sent")
                    stats["alerts_sent"] = cursor.fetchone()[0]
        else:
            stats["alerts_sent"] = "N/A (no DB connection)"
    except Exception:
        stats["alerts_sent"] = "N/A (DB unavailable)"

    return stats


async def post_init(application: Application):
    """Register command menu with Telegram after bot starts."""
    await application.bot.set_my_commands(BOT_COMMANDS)
    logger.info("Bot command menu registered")


def main():
    """Start the bot with long-polling."""
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )

    if not TELEGRAM_BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN not set")
        return

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).post_init(post_init).build()

    # Register command handlers
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("filters", cmd_filters))
    app.add_handler(CommandHandler("seniority", cmd_seniority))
    app.add_handler(CommandHandler("tech", cmd_tech))
    app.add_handler(CommandHandler("latest", cmd_latest))
    app.add_handler(CommandHandler("stats", cmd_stats))

    logger.info("Bot starting (long-polling)...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
