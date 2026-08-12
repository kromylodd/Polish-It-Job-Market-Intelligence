"""Tests for analytics opt-out behavior and new-user counting."""

import pytest

from telegram_bot import analytics


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    """Point analytics at a throwaway SQLite file (per-call connections, no state)."""
    monkeypatch.setattr(analytics, "DB_PATH", tmp_path / "analytics_test.db")
    yield analytics


def test_new_user_detected_once(fresh_db):
    assert fresh_db.log_command(111, "start") is True
    assert fresh_db.log_command(111, "help") is False


def test_opt_in_records_commands_and_filters(fresh_db):
    fresh_db.log_command(222, "latest")
    fresh_db.log_filter_choice(222, "technology", ["Python", "SQL"])

    summary = fresh_db.get_analytics_summary()
    assert summary["total_users"] == 1
    assert summary["opted_out_users"] == 0
    assert summary["commands"].get("latest") == 1
    assert summary["top_technologies"].get("Python") == 1
    assert summary["top_technologies"].get("SQL") == 1


def test_opt_out_suppresses_command_logging(fresh_db):
    fresh_db.set_opt_out(333, True)
    assert fresh_db.is_opted_out(333) is True

    # Command is not recorded as an event, but the user still counts.
    fresh_db.log_command(333, "latest")
    fresh_db.log_filter_choice(333, "technology", ["Python"])

    summary = fresh_db.get_analytics_summary()
    assert summary["total_users"] == 1
    assert summary["opted_out_users"] == 1
    assert summary["total_events"] == 0
    assert summary["commands"] == {}
    assert summary["top_technologies"] == {}


def test_opt_out_then_opt_in_resumes_logging(fresh_db):
    fresh_db.set_opt_out(444, True)
    fresh_db.log_command(444, "help")  # suppressed
    fresh_db.set_opt_out(444, False)
    fresh_db.log_command(444, "help")  # recorded

    summary = fresh_db.get_analytics_summary()
    assert summary["total_events"] == 1
    assert summary["commands"].get("help") == 1


def test_hash_is_deterministic_and_non_raw(fresh_db):
    h1 = fresh_db._hash_user(555)
    h2 = fresh_db._hash_user(555)
    assert h1 == h2
    assert "555" not in h1
    assert len(h1) == 16


def test_reset_preserves_feedback_but_wipes_counters(fresh_db):
    fresh_db.log_command(1, "start")
    fresh_db.log_filter_choice(1, "technology", ["Python"])
    fresh_db.log_feedback(1, "please add Rust filters")

    result = fresh_db.reset_analytics()
    assert result["events_deleted"] >= 1  # the command event
    assert result["filter_choices_deleted"] >= 1

    summary = fresh_db.get_analytics_summary()
    assert summary["total_events"] == 0  # command counters wiped
    assert summary["commands"] == {}
    assert summary["top_technologies"] == {}
    assert summary["total_users"] == 1  # users preserved

    # Feedback row must survive the reset.
    import sqlite3

    conn = sqlite3.connect(str(fresh_db.DB_PATH))
    n = conn.execute(
        "SELECT COUNT(*) FROM events WHERE event_type = ?", (fresh_db.FEEDBACK_EVENT,)
    ).fetchone()[0]
    conn.close()
    assert n == 1


def test_feedback_logged_even_when_opted_out(fresh_db):
    fresh_db.set_opt_out(2, True)
    fresh_db.log_feedback(2, "hi")
    import sqlite3

    conn = sqlite3.connect(str(fresh_db.DB_PATH))
    n = conn.execute(
        "SELECT COUNT(*) FROM events WHERE event_type = ?", (fresh_db.FEEDBACK_EVENT,)
    ).fetchone()[0]
    conn.close()
    assert n == 1
