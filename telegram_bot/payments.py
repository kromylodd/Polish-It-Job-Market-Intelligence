"""
Subscription / paywall logic using Telegram Stars (currency ``XTR``).

Telegram Stars are the native in-app currency for digital goods; a bot sells
them with ``send_invoice(currency="XTR", provider_token="")`` — no Stripe or any
third-party provider needed, and Telegram handles the checkout UI. This module
owns the *state and rules* (tiers, features, expiry) in a small SQLite DB; the
actual invoice send + payment callbacks live in bot.py (they need the running
Application), but they call into here to activate a subscription.

Tiers (monthly):
  Free            — daily digest + all filters (free, no entry here)
  Plus (250 ⭐)   — saved-filter push, /latest anytime, /salary, /trend
  Pro  (600 ⭐)   — everything in Plus + /skills co-occurrence, /company intel,
                     /export, application tracker, weekly report

Pro is a superset of Plus (feature checks respect the hierarchy). A price of N
Stars is passed to Telegram as an integer amount of N (XTR has 0 decimal places).

Lifecycle: purchases don't auto-renew, so after expiry there's a short grace
window (``GRACE_DAYS``) during which access continues while the user is nudged
to renew; a background job sends renewal reminders (``due_for_reminder`` /
``mark_reminded``), and ``refund_payment`` records a Stars refund and revokes
access (the actual ``refundStarPayment`` API call is made in bot.py).
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
        "PAYMENTS_DB_PATH",
        str(Path(__file__).parent / "payments.db"),
    )
)

_lock = threading.Lock()

# --- Lifecycle tunables -----------------------------------------------------
# Grace period after expiry during which access continues while the user is
# nudged to renew (Stars purchases don't auto-renew, so this softens the cliff).
GRACE_DAYS = int(os.environ.get("SUBSCRIPTION_GRACE_DAYS", "3"))
# Start sending renewal reminders this many days before expiry.
RENEWAL_REMIND_DAYS = int(os.environ.get("SUBSCRIPTION_REMIND_DAYS", "3"))
# Don't remind the same user more often than this (seconds).
_REMIND_COOLDOWN_SECONDS = int(os.environ.get("SUBSCRIPTION_REMIND_COOLDOWN", str(20 * 3600)))

# Feature keys used for gating individual commands.
FEATURE_FILTER_PUSH = "filter_push"
FEATURE_LATEST = "latest"
FEATURE_SALARY = "salary"
FEATURE_TREND = "trend"
FEATURE_SKILLS = "skills"
FEATURE_COMPANY = "company"
FEATURE_EXPORT = "export"
FEATURE_TRACKER = "tracker"
FEATURE_REPORT = "report"

_PLUS_FEATURES = {
    FEATURE_FILTER_PUSH,
    FEATURE_LATEST,
    FEATURE_SALARY,
    FEATURE_TREND,
}
_PRO_FEATURES = _PLUS_FEATURES | {
    FEATURE_SKILLS,
    FEATURE_COMPANY,
    FEATURE_EXPORT,
    FEATURE_TRACKER,
    FEATURE_REPORT,
}

# Ordered low→high so a higher tier satisfies a lower-tier requirement.
TIERS: dict[str, dict] = {
    "plus": {
        "name": "Plus",
        "stars": 250,
        "days": 30,
        "rank": 1,
        "features": _PLUS_FEATURES,
        "blurb": "Saved-filter push · /latest anytime · /salary · /trend",
    },
    "pro": {
        "name": "Pro",
        "stars": 600,
        "days": 30,
        "rank": 2,
        "features": _PRO_FEATURES,
        "blurb": (
            "Everything in Plus · /skills co-occurrence · /company intel · "
            "/export · application tracker · weekly report"
        ),
    },
}


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def _init(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS subscriptions (
            chat_id     INTEGER PRIMARY KEY,
            tier        TEXT    NOT NULL,
            expires_at  REAL    NOT NULL,
            updated_at  REAL    NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS payments (
            charge_id   TEXT PRIMARY KEY,
            chat_id     INTEGER NOT NULL,
            tier        TEXT    NOT NULL,
            stars       INTEGER NOT NULL,
            paid_at     REAL    NOT NULL
        )
        """
    )
    # Lightweight migrations for the lifecycle columns (older DBs won't have them).
    for table, col, decl in (
        ("subscriptions", "last_reminded_at", "REAL NOT NULL DEFAULT 0"),
        ("payments", "refunded_at", "REAL"),
    ):
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
        except sqlite3.OperationalError:
            pass  # column already exists — expected on the happy path
    conn.commit()


def _status(expires_at: float, now: float) -> str | None:
    """Lifecycle status for an expiry: 'active', 'grace', or None (fully expired)."""
    if now < expires_at:
        return "active"
    if now < expires_at + GRACE_DAYS * 86400:
        return "grace"
    return None


def activate(chat_id: int, tier: str, *, days: int | None = None) -> float:
    """Activate/extend a subscription. Returns the new expiry epoch.

    If the user already has time left on any tier, we extend from the later of
    ``now`` and the current expiry so paying again stacks fairly.
    """
    if tier not in TIERS:
        raise ValueError(f"unknown tier {tier!r}")
    days = days if days is not None else TIERS[tier]["days"]
    now = time.time()
    with _lock:
        conn = _connect()
        try:
            _init(conn)
            row = conn.execute(
                "SELECT expires_at FROM subscriptions WHERE chat_id=?", (chat_id,)
            ).fetchone()
            base = max(now, row["expires_at"]) if row else now
            expires = base + days * 86400
            conn.execute(
                "INSERT INTO subscriptions (chat_id, tier, expires_at, updated_at) "
                "VALUES (?,?,?,?) "
                "ON CONFLICT(chat_id) DO UPDATE SET tier=excluded.tier, "
                "expires_at=excluded.expires_at, updated_at=excluded.updated_at",
                (chat_id, tier, expires, now),
            )
            conn.commit()
            return expires
        finally:
            conn.close()


def record_payment(charge_id: str, chat_id: int, tier: str, stars: int) -> None:
    """Persist a successful Stars charge (idempotent on charge_id)."""
    with _lock:
        conn = _connect()
        try:
            _init(conn)
            conn.execute(
                "INSERT OR IGNORE INTO payments (charge_id, chat_id, tier, stars, paid_at) "
                "VALUES (?,?,?,?,?)",
                (charge_id, chat_id, tier, stars, time.time()),
            )
            conn.commit()
        finally:
            conn.close()


def get_subscription(chat_id: int) -> dict | None:
    """Return the active/grace subscription or None if none/fully-expired.

    The result includes a lifecycle ``status`` ('active' or 'grace') and an
    ``in_grace`` flag. Access continues through the grace window (see
    ``GRACE_DAYS``); only once past grace is the subscription considered gone.
    """
    with _lock:
        conn = _connect()
        try:
            _init(conn)
            row = conn.execute(
                "SELECT tier, expires_at FROM subscriptions WHERE chat_id=?", (chat_id,)
            ).fetchone()
        finally:
            conn.close()
    if not row:
        return None
    status = _status(row["expires_at"], time.time())
    if status is None:
        return None
    return {
        "tier": row["tier"],
        "expires_at": row["expires_at"],
        "status": status,
        "in_grace": status == "grace",
    }


def is_subscribed(chat_id: int, tier: str | None = None) -> bool:
    """Whether the user has an active subscription (optionally of at least ``tier``)."""
    sub = get_subscription(chat_id)
    if not sub:
        return False
    if tier is None:
        return True
    return TIERS[sub["tier"]]["rank"] >= TIERS[tier]["rank"]


def has_feature(chat_id: int, feature: str) -> bool:
    """Whether the user's active tier includes ``feature``."""
    sub = get_subscription(chat_id)
    if not sub:
        return False
    return feature in TIERS[sub["tier"]]["features"]


def tier_for_payload(payload: str) -> str | None:
    """Extract the tier from an invoice payload of the form ``sub:<tier>:<chat_id>``."""
    parts = payload.split(":")
    if len(parts) >= 2 and parts[0] == "sub" and parts[1] in TIERS:
        return parts[1]
    return None


def make_payload(tier: str, chat_id: int) -> str:
    """Build the invoice payload encoding the tier + buyer."""
    return f"sub:{tier}:{chat_id}"


# --- Refunds ---------------------------------------------------------------


def get_payment(charge_id: str) -> dict | None:
    """Return a payment record by Telegram charge id, or None if unknown."""
    with _lock:
        conn = _connect()
        try:
            _init(conn)
            row = conn.execute(
                "SELECT charge_id, chat_id, tier, stars, paid_at, refunded_at "
                "FROM payments WHERE charge_id=?",
                (charge_id,),
            ).fetchone()
        finally:
            conn.close()
    return dict(row) if row else None


def refund_payment(charge_id: str) -> dict | None:
    """Mark a charge refunded and revoke the buyer's subscription.

    Returns the payment record (so the caller can issue the actual Telegram
    ``refundStarPayment`` API call), or None if the charge is unknown or was
    already refunded. Refunding revokes access immediately (simple, no
    proration — a full refund ends the subscription).
    """
    now = time.time()
    with _lock:
        conn = _connect()
        try:
            _init(conn)
            row = conn.execute(
                "SELECT charge_id, chat_id, tier, stars, paid_at, refunded_at "
                "FROM payments WHERE charge_id=?",
                (charge_id,),
            ).fetchone()
            if not row or row["refunded_at"] is not None:
                return None
            conn.execute("UPDATE payments SET refunded_at=? WHERE charge_id=?", (now, charge_id))
            # Revoke access for the buyer (full refund ends the subscription).
            conn.execute("DELETE FROM subscriptions WHERE chat_id=?", (row["chat_id"],))
            conn.commit()
            return dict(row)
        finally:
            conn.close()


# --- Renewal reminders -----------------------------------------------------


def due_for_reminder(now: float | None = None) -> list[dict]:
    """Subscriptions that should get a renewal nudge right now.

    A subscription is due when it's within ``RENEWAL_REMIND_DAYS`` of expiry (or
    already in the grace window), isn't fully expired, and hasn't been reminded
    within the cooldown. Each row: ``chat_id``, ``tier``, ``expires_at``,
    ``status`` ('active' near expiry, or 'grace').
    """
    now = time.time() if now is None else now
    remind_from = RENEWAL_REMIND_DAYS * 86400
    grace_end_offset = GRACE_DAYS * 86400
    with _lock:
        conn = _connect()
        try:
            _init(conn)
            rows = conn.execute(
                "SELECT chat_id, tier, expires_at FROM subscriptions "
                "WHERE (expires_at - ?) <= ? "  # within the pre-expiry reminder window
                "  AND (expires_at + ?) > ? "  # not fully past grace
                "  AND (last_reminded_at IS NULL OR (? - last_reminded_at) > ?)",
                (remind_from, now, grace_end_offset, now, now, _REMIND_COOLDOWN_SECONDS),
            ).fetchall()
        finally:
            conn.close()
    due = []
    for r in rows:
        status = _status(r["expires_at"], now)
        if status is None:
            continue
        due.append(
            {
                "chat_id": r["chat_id"],
                "tier": r["tier"],
                "expires_at": r["expires_at"],
                "status": status,
            }
        )
    return due


def mark_reminded(chat_id: int, ts: float | None = None) -> None:
    """Record that a renewal reminder was sent to ``chat_id`` (cooldown anchor)."""
    ts = time.time() if ts is None else ts
    with _lock:
        conn = _connect()
        try:
            _init(conn)
            conn.execute(
                "UPDATE subscriptions SET last_reminded_at=? WHERE chat_id=?", (ts, chat_id)
            )
            conn.commit()
        finally:
            conn.close()
