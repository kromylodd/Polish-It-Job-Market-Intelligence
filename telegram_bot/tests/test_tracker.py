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


def test_list_page_totals_and_slicing(tracker):
    for i in range(15):
        tracker.set_status(1, f"job{i:02d}", "applied")
    page0, total = tracker.list_page(1, None, 10, 0)
    assert total == 15
    assert len(page0) == 10
    page1, total1 = tracker.list_page(1, None, 10, 10)
    assert total1 == 15
    assert len(page1) == 5
    # No overlap between pages.
    ids0 = {r["listing_id"] for r in page0}
    ids1 = {r["listing_id"] for r in page1}
    assert ids0.isdisjoint(ids1)


def test_list_page_order_stable_across_status_update(tracker):
    for i in range(15):
        tracker.set_status(1, f"job{i:02d}", "applied")
    page0_before = [r["listing_id"] for r in tracker.list_page(1, None, 10, 0)[0]]
    # Re-mark an item on page 0: updates updated_at but NOT created_at, so the
    # created_at-ordered page must not shift (the pagination-instability bug).
    tracker.set_status(1, page0_before[0], "rejected")
    page0_after = [r["listing_id"] for r in tracker.list_page(1, None, 10, 0)[0]]
    assert page0_after == page0_before


def test_list_page_status_filter(tracker):
    tracker.set_status(1, "a", "applied")
    tracker.set_status(1, "b", "interested")
    tracker.set_status(1, "c", "applied")
    rows, total = tracker.list_page(1, "applied", 10, 0)
    assert total == 2
    assert all(r["status"] == "applied" for r in rows)
