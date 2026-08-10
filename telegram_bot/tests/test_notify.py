"""Tests for the daily-broadcast per-user listing cap."""

from telegram_bot import notify


def test_cap_for_defaults_to_free():
    # No max_listings published (free user) => the free default.
    assert notify._cap_for({}) == notify.MAX_PER_USER


def test_cap_for_reads_published_cap():
    # The bot stamps a paid user's cap into the shared config.
    assert notify._cap_for({"max_listings": 100}) == 100
    assert notify._cap_for({"max_listings": 50}) == 50


def test_cap_for_guards_bad_values():
    assert notify._cap_for({"max_listings": 0}) == notify.MAX_PER_USER
    assert notify._cap_for({"max_listings": -5}) == notify.MAX_PER_USER
    assert notify._cap_for({"max_listings": "junk"}) == notify.MAX_PER_USER
