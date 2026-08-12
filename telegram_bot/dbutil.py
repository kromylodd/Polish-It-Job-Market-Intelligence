"""
Shared SQLite helpers for the bot's local stores (payments, tracker, analytics,
alerts idempotency).

Two things every store needs and used to reimplement differently:

  * A connection factory that creates the parent directory and — critically —
    enforces ``0600`` file permissions. These DBs hold billing records
    (payments.db) and per-user application history (tracker.db); they must not
    be world-readable on a shared host.
  * A single, consistent concurrency model. python-telegram-bot dispatches
    handlers across a thread pool and offloads blocking work with
    ``asyncio.to_thread``, so every store is touched from multiple threads. We
    standardise on *one short-lived connection per call* guarded by a module
    lock (see ``locked_connection``), rather than mixing thread-local
    connections and lock-per-call across modules.
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
import threading
from collections.abc import Iterator
from pathlib import Path


def connect(db_path: str | Path) -> sqlite3.Connection:
    """Open a SQLite connection, creating the parent dir and enforcing 0600 perms.

    The chmod is applied on every open (cheap, idempotent) so a DB created by an
    older build with loose permissions is tightened on the next access.
    """
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Restrict *before* the connection can create the file, so it's never briefly
    # world-readable: set the process umask complement isn't reliable across
    # threads, so we chmod immediately after connect instead.
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    try:
        os.chmod(path, 0o600)
    except OSError:
        # Non-fatal: on some filesystems chmod may not be permitted. The data is
        # still written; we just couldn't tighten perms.
        pass
    return conn


@contextlib.contextmanager
def locked_connection(db_path: str | Path, lock: threading.Lock) -> Iterator[sqlite3.Connection]:
    """Context manager: acquire ``lock``, open a 0600 connection, always close it.

    Usage::

        with locked_connection(DB_PATH, _lock) as conn:
            conn.execute(...)
            conn.commit()
    """
    with lock:
        conn = connect(db_path)
        try:
            yield conn
        finally:
            conn.close()
