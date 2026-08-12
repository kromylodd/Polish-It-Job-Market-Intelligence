"""Tests for the daily-broadcast per-user listing cap (derived from payments)."""

import importlib

import pytest


@pytest.fixture
def notify(tmp_path, monkeypatch):
    # Point payments at a throwaway DB so subscription state is isolated.
    monkeypatch.setenv("PAYMENTS_DB_PATH", str(tmp_path / "payments.db"))
    import telegram_bot.payments as payments_mod

    importlib.reload(payments_mod)
    import telegram_bot.notify as notify_mod

    return importlib.reload(notify_mod)


def test_cap_defaults_to_free_for_unknown_user(notify):
    # No subscription => the free default.
    assert notify._cap_for_chat(12345) == notify.payments.FREE_MAX_LISTINGS


def test_cap_guards_bad_chat_id(notify):
    assert notify._cap_for_chat("junk") == notify.MAX_PER_USER
    assert notify._cap_for_chat(None) == notify.MAX_PER_USER


def test_cap_reflects_subscription_tier(notify):
    notify.payments.activate(777, "plus")
    assert notify._cap_for_chat(777) == notify.payments.TIERS["plus"]["max_listings"]
    notify.payments.activate(888, "pro")
    assert notify._cap_for_chat("888") == notify.payments.TIERS["pro"]["max_listings"]


def test_free_default_matches_payments(notify):
    # The two modules must agree on the free cap (no drift).
    assert notify.MAX_PER_USER == notify.payments.FREE_MAX_LISTINGS
