"""
Personal application tracker (premium retention feature).

Lets a user mark listings as ``applied`` / ``interested`` / ``rejected`` and
review them later. This turns the bot from a broadcast channel into a personal
tool users keep coming back to — the single biggest retention lever for a paid
tier.

State is per-``chat_id`` in a small SQLite DB (one row per (chat_id, listing_id);
re-marking updates the status). No PII beyond the Telegram chat_id, which the
user already shares by messaging the bot.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

DB_PATH = Path(
    os.environ.get(
        "TRACKER_DB_PATH",
        str(Path(__file__).parent / "tracker.db"),
    )
)

VALID_STATUSES = ("applied", "interested", "rejected")

_lock = threading.Lock()


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def _init(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS applications (
            chat_id     INTEGER NOT NULL,
            listing_id  TEXT    NOT NULL,
            title       TEXT,
            company     TEXT,
            url         TEXT,
            status      TEXT    NOT NULL,
            created_at  REAL    NOT NULL,
            updated_at  REAL    NOT NULL,
            PRIMARY KEY (chat_id, listing_id)
        )
        """
    )
    conn.commit()


def set_status(
    chat_id: int,
    listing_id: str,
    status: str,
    *,
    title: str | None = None,
    company: str | None = None,
    url: str | None = None,
) -> bool:
    """Insert or update the tracked status for a listing. Returns True on success.

    Idempotent upsert keyed on (chat_id, listing_id). Metadata (title/company/url)
    is only overwritten when a non-None value is supplied, so a status-only update
    from an inline button doesn't wipe previously stored details.
    """
    if status not in VALID_STATUSES:
        raise ValueError(f"invalid status {status!r}; expected one of {VALID_STATUSES}")
    now = time.time()
    with _lock:
        conn = _connect()
        try:
            _init(conn)
            existing = conn.execute(
                "SELECT title, company, url, created_at FROM applications "
                "WHERE chat_id=? AND listing_id=?",
                (chat_id, str(listing_id)),
            ).fetchone()
            if existing:
                conn.execute(
                    "UPDATE applications SET status=?, title=?, company=?, url=?, updated_at=? "
                    "WHERE chat_id=? AND listing_id=?",
                    (
                        status,
                        title if title is not None else existing["title"],
                        company if company is not None else existing["company"],
                        url if url is not None else existing["url"],
                        now,
                        chat_id,
                        str(listing_id),
                    ),
                )
            else:
                conn.execute(
                    "INSERT INTO applications "
                    "(chat_id, listing_id, title, company, url, status, created_at, updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (chat_id, str(listing_id), title, company, url, status, now, now),
                )
            conn.commit()
            return True
        except Exception as e:
            logger.error("tracker set_status failed: %s", e)
            return False
        finally:
            conn.close()


def list_applications(chat_id: int, status: str | None = None) -> list[dict]:
    """Return a user's tracked listings, optionally filtered by status.

    Ordered most-recently-updated first.
    """
    with _lock:
        conn = _connect()
        try:
            _init(conn)
            if status:
                rows = conn.execute(
                    "SELECT * FROM applications WHERE chat_id=? AND status=? "
                    "ORDER BY updated_at DESC",
                    (chat_id, status),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM applications WHERE chat_id=? ORDER BY updated_at DESC",
                    (chat_id,),
                ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()


def counts(chat_id: int) -> dict[str, int]:
    """Return {status: count} for a user (statuses with 0 omitted)."""
    with _lock:
        conn = _connect()
        try:
            _init(conn)
            rows = conn.execute(
                "SELECT status, count(*) AS n FROM applications WHERE chat_id=? GROUP BY status",
                (chat_id,),
            ).fetchall()
            return {r["status"]: r["n"] for r in rows}
        finally:
            conn.close()


def remove(chat_id: int, listing_id: str) -> bool:
    """Delete a tracked listing. Returns True if a row was removed."""
    with _lock:
        conn = _connect()
        try:
            _init(conn)
            cur = conn.execute(
                "DELETE FROM applications WHERE chat_id=? AND listing_id=?",
                (chat_id, str(listing_id)),
            )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()


def tracked_listing_ids(chat_id: int) -> set[str]:
    """Return the set of listing IDs this user has interacted with (any status)."""
    with _lock:
        conn = _connect()
        try:
            _init(conn)
            rows = conn.execute(
                "SELECT listing_id FROM applications WHERE chat_id=?",
                (chat_id,),
            ).fetchall()
            return {row[0] for row in rows}
        finally:
            conn.close()
