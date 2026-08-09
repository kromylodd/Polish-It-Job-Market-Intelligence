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
    # Activate with a negative duration => already expired.
    payments.activate(1, "pro", days=-1)
    assert payments.get_subscription(1) is None
    assert payments.is_subscribed(1) is False


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
