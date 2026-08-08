"""Tests for analytics opt-out behavior and new-user counting."""

import pytest

from telegram_bot import analytics


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    """Point analytics at a throwaway SQLite file and reset the thread-local conn."""
    monkeypatch.setattr(analytics, "DB_PATH", tmp_path / "analytics_test.db")

    def _reset():
        conn = getattr(analytics._local, "conn", None)
        if conn is not None:
            conn.close()
            del analytics._local.conn

    _reset()
    yield analytics
    _reset()


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
