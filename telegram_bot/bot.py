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

from telegram import Update
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

DEFAULT_CONFIG = {
    "seniorities": ["junior", "mid"],
    "technologies": ["Python", "SQL", "Apache Spark", "dbt", "Apache Airflow"],
}


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
        "Commands:\n"
        "/filters — view your current alert settings\n"
        "/seniority — change seniority filter\n"
        "/tech — change technology filter\n"
        "/latest — get recent matching listings now\n"
        "/stats — pipeline statistics\n"
        "/help — show all commands"
    )
    await update.message.reply_text(text, parse_mode="HTML")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /help command."""
    text = (
        "<b>Available commands:</b>\n\n"
        "/start — welcome + overview\n"
        "/filters — show current filter settings\n"
        "/seniority &lt;levels&gt; — set seniority filter\n"
        "  <i>e.g. /seniority junior mid senior</i>\n"
        "/tech &lt;technologies&gt; — set technology filter\n"
        "  <i>e.g. /tech Python SQL dbt Airflow</i>\n"
        "/latest — fetch most recent matching listings\n"
        "/stats — pipeline & alert statistics\n"
        "/help — this message\n\n"
        "Filters affect both daily alerts and /latest results."
    )
    await update.message.reply_text(text, parse_mode="HTML")


async def cmd_filters(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /filters command — show current settings."""
    config = load_config()
    seniorities = ", ".join(config.get("seniorities", []))
    technologies = ", ".join(config.get("technologies", []))
    text = (
        "<b>Current alert filters:</b>\n\n"
        f"🎯 Seniority: {seniorities or 'any'}\n"
        f"💻 Technologies: {technologies or 'any'}\n\n"
        "Change with /seniority or /tech"
    )
    await update.message.reply_text(text, parse_mode="HTML")


async def cmd_seniority(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /seniority command — set seniority filter."""
    args = context.args
    valid_levels = {"junior", "mid", "senior", "lead", "manager"}

    if not args:
        config = load_config()
        current = ", ".join(config.get("seniorities", []))
        await update.message.reply_text(
            f"Current seniority filter: <b>{current or 'any'}</b>\n\n"
            f"Usage: /seniority junior mid senior\n"
            f"Valid: {', '.join(sorted(valid_levels))}",
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
    await update.message.reply_text(
        f"✅ Seniority filter updated: <b>{', '.join(levels)}</b>",
        parse_mode="HTML",
    )


async def cmd_tech(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /tech command — set technology filter."""
    args = context.args

    if not args:
        config = load_config()
        current = ", ".join(config.get("technologies", []))
        await update.message.reply_text(
            f"Current technology filter: <b>{current or 'any'}</b>\n\n"
            f"Usage: /tech Python SQL dbt Airflow Spark\n"
            f"Separate each tech with a space.",
            parse_mode="HTML",
        )
        return

    # Accept as-is (case-sensitive, tech names are proper nouns)
    config = load_config()
    config["technologies"] = list(args)
    save_config(config)
    await update.message.reply_text(
        f"✅ Technology filter updated: <b>{', '.join(args)}</b>",
        parse_mode="HTML",
    )


async def cmd_latest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /latest — fetch recent listings from local data or Databricks."""
    await update.message.reply_text("🔍 Fetching latest listings...\n")

    config = load_config()
    listings = _get_latest_listings(config)

    if not listings:
        await update.message.reply_text(
            "No recent listings match your current filters.\n"
            "Try broadening with /seniority or /tech.",
        )
        return

    header = f"📋 <b>{len(listings)} recent match{'es' if len(listings) != 1 else ''}:</b>\n"
    await update.message.reply_text(header, parse_mode="HTML")

    import asyncio

    from telegram_bot.notify import format_listing

    for listing in listings[:10]:
        await update.message.reply_text(
            format_listing(listing),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
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
            # Extract date from filename: raw_listings_YYYYMMDD_HHMMSS.json
            stem = latest.stem  # raw_listings_20260808_060000
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


def main():
    """Start the bot with long-polling."""
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )

    if not TELEGRAM_BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN not set")
        return

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

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
