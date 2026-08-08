"""
Anonymous usage analytics for the Telegram bot.

Tracks aggregated, non-personal statistics:
- Unique user count (chat_id hashed, not stored raw)
- Command usage frequency
- Popular filter values (technologies, categories, cities, seniorities)
- /latest query count

No personally identifiable information is stored. Chat IDs are hashed
with SHA-256 before storage so individual users cannot be identified.

Storage: local SQLite database (telegram_bot/analytics.db).
"""

import hashlib
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(__file__).parent / "analytics.db"

_local = threading.local()


def _get_conn() -> sqlite3.Connection:
    """Get a thread-local SQLite connection."""
    if not hasattr(_local, "conn"):
        _local.conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        _init_db(_local.conn)
    return _local.conn


def _init_db(conn: sqlite3.Connection):
    """Create tables if they don't exist."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            user_hash TEXT NOT NULL,
            event_type TEXT NOT NULL,
            event_data TEXT
        );

        CREATE TABLE IF NOT EXISTS filter_choices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            user_hash TEXT NOT NULL,
            dimension TEXT NOT NULL,
            value TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_events_type ON events(event_type);
        CREATE INDEX IF NOT EXISTS idx_filter_dimension ON filter_choices(dimension);
    """
    )
    conn.commit()


def _hash_user(chat_id: int) -> str:
    """Hash chat_id with SHA-256. One-way, cannot be reversed."""
    return hashlib.sha256(str(chat_id).encode()).hexdigest()[:16]


def log_command(chat_id: int, command: str):
    """Log a command usage event."""
    conn = _get_conn()
    now = datetime.now(timezone.utc).isoformat()
    user_hash = _hash_user(chat_id)
    conn.execute(
        "INSERT INTO events (timestamp, user_hash, event_type, event_data) VALUES (?, ?, ?, ?)",
        (now, user_hash, "command", command),
    )
    conn.commit()


def log_filter_choice(chat_id: int, dimension: str, values: list[str]):
    """Log filter choices (e.g. which techs/cities a user selected)."""
    if not values:
        return
    conn = _get_conn()
    now = datetime.now(timezone.utc).isoformat()
    user_hash = _hash_user(chat_id)
    rows = [(now, user_hash, dimension, v) for v in values]
    conn.executemany(
        "INSERT INTO filter_choices (timestamp, user_hash, dimension, value) VALUES (?, ?, ?, ?)",
        rows,
    )
    conn.commit()


def get_analytics_summary() -> dict:
    """Get aggregated analytics for the /analytics command."""
    conn = _get_conn()

    summary = {}

    # Unique users
    row = conn.execute("SELECT COUNT(DISTINCT user_hash) FROM events").fetchone()
    summary["total_users"] = row[0] if row else 0

    # Total events
    row = conn.execute("SELECT COUNT(*) FROM events").fetchone()
    summary["total_events"] = row[0] if row else 0

    # Command usage breakdown
    rows = conn.execute(
        "SELECT event_data, COUNT(*) as cnt FROM events "
        "WHERE event_type = 'command' GROUP BY event_data ORDER BY cnt DESC"
    ).fetchall()
    summary["commands"] = {row[0]: row[1] for row in rows}

    # Top technologies
    rows = conn.execute(
        "SELECT value, COUNT(*) as cnt FROM filter_choices "
        "WHERE dimension = 'technology' GROUP BY value ORDER BY cnt DESC LIMIT 10"
    ).fetchall()
    summary["top_technologies"] = {row[0]: row[1] for row in rows}

    # Top categories
    rows = conn.execute(
        "SELECT value, COUNT(*) as cnt FROM filter_choices "
        "WHERE dimension = 'category' GROUP BY value ORDER BY cnt DESC LIMIT 10"
    ).fetchall()
    summary["top_categories"] = {row[0]: row[1] for row in rows}

    # Top cities
    rows = conn.execute(
        "SELECT value, COUNT(*) as cnt FROM filter_choices "
        "WHERE dimension = 'city' GROUP BY value ORDER BY cnt DESC LIMIT 10"
    ).fetchall()
    summary["top_cities"] = {row[0]: row[1] for row in rows}

    # Top seniorities
    rows = conn.execute(
        "SELECT value, COUNT(*) as cnt FROM filter_choices "
        "WHERE dimension = 'seniority' GROUP BY value ORDER BY cnt DESC"
    ).fetchall()
    summary["top_seniorities"] = {row[0]: row[1] for row in rows}

    # Top workplaces
    rows = conn.execute(
        "SELECT value, COUNT(*) as cnt FROM filter_choices "
        "WHERE dimension = 'workplace' GROUP BY value ORDER BY cnt DESC"
    ).fetchall()
    summary["top_workplaces"] = {row[0]: row[1] for row in rows}

    return summary
