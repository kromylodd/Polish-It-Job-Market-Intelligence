"""Tests for the SQLite application tracker."""

import importlib

import pytest


@pytest.fixture
def tracker(tmp_path, monkeypatch):
    monkeypatch.setenv("TRACKER_DB_PATH", str(tmp_path / "tracker.db"))
    import telegram_bot.tracker as tracker_mod

    return importlib.reload(tracker_mod)


def test_set_and_list(tracker):
    tracker.set_status(1, "job1", "applied", title="Dev", company="Acme", url="http://x")
    apps = tracker.list_applications(1)
    assert len(apps) == 1
    assert apps[0]["status"] == "applied"
    assert apps[0]["title"] == "Dev"


def test_status_update_preserves_metadata(tracker):
    tracker.set_status(1, "job1", "interested", title="Dev", company="Acme")
    # Status-only update (e.g. from an inline button) must not wipe title/company.
    tracker.set_status(1, "job1", "applied")
    apps = tracker.list_applications(1)
    assert len(apps) == 1
    assert apps[0]["status"] == "applied"
    assert apps[0]["title"] == "Dev"
    assert apps[0]["company"] == "Acme"


def test_per_user_isolation(tracker):
    tracker.set_status(1, "job1", "applied")
    tracker.set_status(2, "job2", "rejected")
    assert len(tracker.list_applications(1)) == 1
    assert len(tracker.list_applications(2)) == 1
    assert tracker.list_applications(1)[0]["listing_id"] == "job1"


def test_filter_by_status(tracker):
    tracker.set_status(1, "a", "applied")
    tracker.set_status(1, "b", "interested")
    tracker.set_status(1, "c", "applied")
    assert len(tracker.list_applications(1, status="applied")) == 2
    assert len(tracker.list_applications(1, status="interested")) == 1


def test_counts(tracker):
    tracker.set_status(1, "a", "applied")
    tracker.set_status(1, "b", "applied")
    tracker.set_status(1, "c", "rejected")
    counts = tracker.counts(1)
    assert counts == {"applied": 2, "rejected": 1}


def test_remove(tracker):
    tracker.set_status(1, "a", "applied")
    assert tracker.remove(1, "a") is True
    assert tracker.remove(1, "a") is False
    assert tracker.list_applications(1) == []


def test_invalid_status_raises(tracker):
    with pytest.raises(ValueError):
        tracker.set_status(1, "a", "maybe")
