"""
Anonymous usage analytics for the Telegram bot.

Tracks aggregated, non-personal statistics:
- Unique user count (chat_id hashed, not stored raw)
- Command usage frequency (if user opted in)
- Popular filter values (if user opted in)

Users can opt out of detailed tracking via /privacy. When opted out,
only a "+1 user" count is recorded — no commands or filter preferences.

No personally identifiable information is stored. Chat IDs are pseudonymized
with a salted HMAC-SHA256 before storage so individual users cannot be
identified (set ANALYTICS_SALT to a random secret).

Storage: local SQLite database (telegram_bot/analytics.db). Connections are
short-lived and guarded by a module lock (see telegram_bot/dbutil) — the same
concurrency model as payments.py / tracker.py, so behaviour is consistent and
the file is created with 0600 permissions.
"""

import hashlib
import hmac
import logging
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from telegram_bot import dbutil

logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).parent / "analytics.db"

# Secret salt for hashing chat ids. chat_ids live in a small, enumerable integer
# space, so an unsalted hash could be brute-forced to confirm a specific user.
# A secret HMAC key defeats that. Set ANALYTICS_SALT in the environment.
_ANALYTICS_SALT = os.environ.get("ANALYTICS_SALT", "")
if not _ANALYTICS_SALT:
    logger.warning(
        "ANALYTICS_SALT is not set — user hashes are guessable from a known chat_id. "
        "Set ANALYTICS_SALT to a random secret for real anonymization."
    )

# Event type used for stored /feedback messages. These rows are preserved by
# reset_analytics() (they're user-submitted content, not disposable counters).
FEEDBACK_EVENT = "feedback"
COMMAND_EVENT = "command"

_lock = threading.Lock()


def _init_db(conn: sqlite3.Connection) -> None:
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

        CREATE TABLE IF NOT EXISTS users (
            user_hash TEXT PRIMARY KEY,
            first_seen TEXT NOT NULL,
            opted_out INTEGER NOT NULL DEFAULT 0
        );

        CREATE INDEX IF NOT EXISTS idx_events_type ON events(event_type);
        CREATE INDEX IF NOT EXISTS idx_filter_dimension ON filter_choices(dimension);
    """
    )
    conn.commit()


def _hash_user(chat_id: int) -> str:
    """Pseudonymize chat_id with a salted HMAC-SHA256 (one-way, not reversible).

    With a secret ANALYTICS_SALT set, the digest cannot be brute-forced back to a
    chat_id by enumerating the (small) chat_id space.
    """
    return hmac.new(_ANALYTICS_SALT.encode(), str(chat_id).encode(), hashlib.sha256).hexdigest()[
        :16
    ]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# --- Internal helpers operating on an already-open connection ---------------
# These take an explicit connection (and assume the caller holds the lock) so
# public methods can compose them without re-acquiring the non-reentrant lock.


def _ensure_user(conn: sqlite3.Connection, user_hash: str) -> bool:
    """Register user if not seen before. Returns True if new user."""
    row = conn.execute("SELECT 1 FROM users WHERE user_hash = ?", (user_hash,)).fetchone()
    if row:
        return False
    conn.execute(
        "INSERT OR IGNORE INTO users (user_hash, first_seen, opted_out) VALUES (?, ?, 0)",
        (user_hash, _now()),
    )
    conn.commit()
    return True


def _is_opted_out(conn: sqlite3.Connection, user_hash: str) -> bool:
    row = conn.execute("SELECT opted_out FROM users WHERE user_hash = ?", (user_hash,)).fetchone()
    return bool(row and row[0])


# --- Public API -------------------------------------------------------------


def is_opted_out(chat_id: int) -> bool:
    """Check if a user has opted out of detailed tracking."""
    with dbutil.locked_connection(DB_PATH, _lock) as conn:
        _init_db(conn)
        return _is_opted_out(conn, _hash_user(chat_id))


def set_opt_out(chat_id: int, opted_out: bool) -> None:
    """Set user's opt-out preference."""
    user_hash = _hash_user(chat_id)
    with dbutil.locked_connection(DB_PATH, _lock) as conn:
        _init_db(conn)
        _ensure_user(conn, user_hash)
        conn.execute(
            "UPDATE users SET opted_out = ? WHERE user_hash = ?",
            (1 if opted_out else 0, user_hash),
        )
        conn.commit()


def log_command(chat_id: int, command: str) -> bool:
    """Log a command usage event. Respects opt-out (only counts user).

    Returns True if this was the user's first-ever interaction (new user).
    """
    user_hash = _hash_user(chat_id)
    with dbutil.locked_connection(DB_PATH, _lock) as conn:
        _init_db(conn)
        is_new = _ensure_user(conn, user_hash)
        if _is_opted_out(conn, user_hash):
            return is_new
        conn.execute(
            "INSERT INTO events (timestamp, user_hash, event_type, event_data) "
            "VALUES (?, ?, ?, ?)",
            (_now(), user_hash, COMMAND_EVENT, command),
        )
        conn.commit()
        return is_new


def log_filter_choice(chat_id: int, dimension: str, values: list[str]) -> None:
    """Log filter choices. Skipped if user opted out."""
    if not values:
        return
    user_hash = _hash_user(chat_id)
    with dbutil.locked_connection(DB_PATH, _lock) as conn:
        _init_db(conn)
        if _is_opted_out(conn, user_hash):
            return
        now = _now()
        conn.executemany(
            "INSERT INTO filter_choices (timestamp, user_hash, dimension, value) "
            "VALUES (?, ?, ?, ?)",
            [(now, user_hash, dimension, v) for v in values],
        )
        conn.commit()


def log_feedback(chat_id: int, text: str) -> None:
    """Persist a /feedback message. Stored regardless of opt-out (the user is
    explicitly submitting it) and preserved across reset_analytics()."""
    user_hash = _hash_user(chat_id)
    with dbutil.locked_connection(DB_PATH, _lock) as conn:
        _init_db(conn)
        conn.execute(
            "INSERT INTO events (timestamp, user_hash, event_type, event_data) "
            "VALUES (?, ?, ?, ?)",
            (_now(), user_hash, FEEDBACK_EVENT, text),
        )
        conn.commit()


def reset_analytics() -> dict[str, int]:
    """Delete usage counters (command events + filter_choices).

    Feedback rows (event_type='feedback') and the users table are preserved —
    feedback is user-submitted content, not a disposable counter. Returns a dict
    with the number of deleted rows per table so the admin can confirm the wipe.
    """
    with dbutil.locked_connection(DB_PATH, _lock) as conn:
        _init_db(conn)
        ev = conn.execute(
            "SELECT COUNT(*) FROM events WHERE event_type != ?", (FEEDBACK_EVENT,)
        ).fetchone()[0]
        fc = conn.execute("SELECT COUNT(*) FROM filter_choices").fetchone()[0]
        conn.execute("DELETE FROM events WHERE event_type != ?", (FEEDBACK_EVENT,))
        conn.execute("DELETE FROM filter_choices")
        conn.commit()
    return {"events_deleted": ev, "filter_choices_deleted": fc}


def get_analytics_summary() -> dict:
    """Get aggregated analytics for the /analytics command."""
    with dbutil.locked_connection(DB_PATH, _lock) as conn:
        _init_db(conn)
        summary: dict = {}

        # Total users (including opted-out — everyone counts)
        summary["total_users"] = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]

        # Opted-out count
        summary["opted_out_users"] = conn.execute(
            "SELECT COUNT(*) FROM users WHERE opted_out = 1"
        ).fetchone()[0]

        # Total command interactions (feedback rows excluded from the counter)
        summary["total_events"] = conn.execute(
            "SELECT COUNT(*) FROM events WHERE event_type = ?", (COMMAND_EVENT,)
        ).fetchone()[0]

        # Command usage breakdown
        rows = conn.execute(
            "SELECT event_data, COUNT(*) as cnt FROM events "
            "WHERE event_type = ? GROUP BY event_data ORDER BY cnt DESC",
            (COMMAND_EVENT,),
        ).fetchall()
        summary["commands"] = {row[0]: row[1] for row in rows}

        def _top(dimension: str, limit: int | None) -> dict:
            sql = (
                "SELECT value, COUNT(*) as cnt FROM filter_choices "
                "WHERE dimension = ? GROUP BY value ORDER BY cnt DESC"
            )
            if limit:
                sql += f" LIMIT {int(limit)}"
            return {r[0]: r[1] for r in conn.execute(sql, (dimension,)).fetchall()}

        summary["top_technologies"] = _top("technology", 10)
        summary["top_categories"] = _top("category", 10)
        summary["top_cities"] = _top("city", 10)
        summary["top_seniorities"] = _top("seniority", None)
        summary["top_workplaces"] = _top("workplace", None)

    return summary
