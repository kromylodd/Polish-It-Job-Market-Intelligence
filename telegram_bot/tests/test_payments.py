"""Tests for the Telegram Stars subscription logic."""

import importlib
import time

import pytest


@pytest.fixture
def payments(tmp_path, monkeypatch):
    monkeypatch.setenv("PAYMENTS_DB_PATH", str(tmp_path / "payments.db"))
    import telegram_bot.payments as payments_mod

    return importlib.reload(payments_mod)


def test_no_subscription_by_default(payments):
    assert payments.get_subscription(1) is None
    assert payments.is_subscribed(1) is False
    assert payments.has_feature(1, payments.FEATURE_SALARY) is False


def test_activate_plus(payments):
    payments.activate(1, "plus")
    assert payments.is_subscribed(1) is True
    assert payments.is_subscribed(1, "plus") is True
    assert payments.is_subscribed(1, "pro") is False  # plus < pro
    assert payments.has_feature(1, payments.FEATURE_SALARY) is True
    assert payments.has_feature(1, payments.FEATURE_SKILLS) is False  # pro-only


def test_activate_pro_superset(payments):
    payments.activate(1, "pro")
    assert payments.is_subscribed(1, "plus") is True  # pro satisfies plus
    assert payments.is_subscribed(1, "pro") is True
    assert payments.has_feature(1, payments.FEATURE_SALARY) is True  # plus feature
    assert payments.has_feature(1, payments.FEATURE_SKILLS) is True
    assert payments.has_feature(1, payments.FEATURE_TRACKER) is True


def test_expiry(payments):
    # Fully expired: past the grace window => no subscription at all.
    payments.activate(1, "pro", days=-(payments.GRACE_DAYS + 1))
    assert payments.get_subscription(1) is None
    assert payments.is_subscribed(1) is False


def test_grace_period_keeps_access(payments):
    # Expired 1 day ago but within the grace window: access continues, flagged.
    payments.activate(1, "pro", days=-1)
    sub = payments.get_subscription(1)
    assert sub is not None
    assert sub["status"] == "grace"
    assert sub["in_grace"] is True
    assert payments.is_subscribed(1) is True
    assert payments.has_feature(1, payments.FEATURE_SKILLS) is True


def test_active_not_in_grace(payments):
    payments.activate(1, "plus", days=30)
    sub = payments.get_subscription(1)
    assert sub["status"] == "active"
    assert sub["in_grace"] is False


def test_refund_revokes_and_is_idempotent(payments):
    payments.record_payment("charge_r", 7, "pro", 600)
    payments.activate(7, "pro")
    assert payments.is_subscribed(7) is True

    rec = payments.refund_payment("charge_r")
    assert rec is not None and rec["chat_id"] == 7
    # Access revoked immediately.
    assert payments.get_subscription(7) is None
    assert payments.is_subscribed(7) is False
    # Second refund is a no-op (already refunded).
    assert payments.refund_payment("charge_r") is None
    # Unknown charge => None.
    assert payments.refund_payment("nope") is None


def test_due_for_reminder(payments):
    now = time.time()
    # Expiring within the reminder window.
    payments.activate(1, "plus", days=1)
    # Far from expiry — should NOT be due.
    payments.activate(2, "pro", days=30)
    # In grace — should be due.
    payments.activate(3, "plus", days=-1)
    # Fully expired — should NOT be due.
    payments.activate(4, "pro", days=-(payments.GRACE_DAYS + 5))

    due_ids = {d["chat_id"] for d in payments.due_for_reminder(now)}
    assert 1 in due_ids
    assert 3 in due_ids
    assert 2 not in due_ids
    assert 4 not in due_ids


def test_reminder_cooldown(payments):
    payments.activate(1, "plus", days=1)
    assert any(d["chat_id"] == 1 for d in payments.due_for_reminder())
    payments.mark_reminded(1)
    # Just reminded — cooldown suppresses another nudge.
    assert not any(d["chat_id"] == 1 for d in payments.due_for_reminder())


def test_extend_stacks(payments):
    exp1 = payments.activate(1, "plus", days=30)
    exp2 = payments.activate(1, "plus", days=30)
    # Second activation extends from the first expiry, not from now.
    assert exp2 > exp1
    assert (exp2 - exp1) == pytest.approx(30 * 86400, abs=5)


def test_payload_roundtrip(payments):
    payload = payments.make_payload("pro", 12345)
    assert payload == "sub:pro:12345"
    assert payments.tier_for_payload(payload) == "pro"
    assert payments.tier_for_payload("garbage") is None
    assert payments.tier_for_payload("sub:unknown:1") is None


def test_record_payment_idempotent(payments):
    payments.record_payment("charge1", 1, "pro", 600)
    payments.record_payment("charge1", 1, "pro", 600)  # duplicate ignored
    import sqlite3

    conn = sqlite3.connect(str(payments.DB_PATH))
    n = conn.execute("SELECT count(*) FROM payments").fetchone()[0]
    conn.close()
    assert n == 1


def test_tiers_pricing(payments):
    assert payments.TIERS["plus"]["stars"] == 250
    assert payments.TIERS["pro"]["stars"] == 600
    assert payments.TIERS["pro"]["rank"] > payments.TIERS["plus"]["rank"]


def test_activate_unknown_tier(payments):
    with pytest.raises(ValueError):
        payments.activate(1, "platinum")


def test_active_subscription_fields(payments):
    payments.activate(1, "plus")
    sub = payments.get_subscription(1)
    assert sub["tier"] == "plus"
    assert sub["expires_at"] > time.time()


def test_listing_cap_by_tier(payments):
    # Free (no subscription) => the free default.
    assert payments.listing_cap(1) == payments.FREE_MAX_LISTINGS
    assert payments.max_listings_for(None) == payments.FREE_MAX_LISTINGS
    # Paid tiers get their larger caps.
    payments.activate(1, "plus")
    assert payments.listing_cap(1) == payments.TIERS["plus"]["max_listings"]
    payments.activate(2, "pro")
    assert payments.listing_cap(2) == payments.TIERS["pro"]["max_listings"]
    # Pro cap is at least Plus cap, both above free.
    assert payments.TIERS["pro"]["max_listings"] >= payments.TIERS["plus"]["max_listings"]
    assert payments.TIERS["plus"]["max_listings"] > payments.FREE_MAX_LISTINGS
