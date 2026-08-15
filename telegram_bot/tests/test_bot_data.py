"""Tests for the bot's local DuckDB serving-cache reader (_query_local_cache).

Guards the historical bug where the query used the unqualified table name
``market_snapshot`` instead of ``gold.mart_market_snapshot`` (so the fast path
silently never returned rows), and verifies we don't depend on pandas at runtime.
"""

import os

import pytest

duckdb = pytest.importorskip("duckdb")

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test")

from telegram_bot import bot, serving  # noqa: E402


def _make_snapshot_db(path):
    con = duckdb.connect(str(path))
    con.execute("CREATE SCHEMA IF NOT EXISTS gold")
    con.execute(
        "CREATE TABLE gold.mart_market_snapshot AS SELECT * FROM (VALUES "
        "('id1','Senior Dev','slug-1','Acme','senior','permanent','remote','python',"
        "10000,20000,'PLN','2026-01-02',['Python','SQL'],['Warszawa']),"
        "('id2','Mid Dev','slug-2','Beta','mid','b2b','hybrid','python',"
        "8000,15000,'PLN','2026-01-01',['Python'],['Kraków'])"
        ") t(listing_id,title,slug,company_name,seniority,employment_type,workplace_type,"
        "category,salary_min,salary_max,currency,posted_date,technologies,cities)"
    )
    con.close()


def test_query_local_cache_reads_gold_snapshot(tmp_path, monkeypatch):
    dbp = tmp_path / "pipeline.duckdb"
    _make_snapshot_db(dbp)
    monkeypatch.setattr(serving, "SERVING_DB_PATH", dbp)

    rows = bot._query_local_cache()
    assert rows is not None
    assert {r["listing_id"] for r in rows} == {"id1", "id2"}
    # Ordered by posted_date DESC.
    assert rows[0]["listing_id"] == "id1"
    # Array columns come back as plain Python lists (no numpy/pandas).
    assert isinstance(rows[0]["technologies"], list)
    assert "Python" in rows[0]["technologies"]


def test_query_local_cache_none_when_not_ready(tmp_path, monkeypatch):
    monkeypatch.setattr(serving, "SERVING_DB_PATH", tmp_path / "missing.duckdb")
    assert bot._query_local_cache() is None


def test_get_latest_listings_filters_snapshot(tmp_path, monkeypatch):
    dbp = tmp_path / "pipeline.duckdb"
    _make_snapshot_db(dbp)
    monkeypatch.setattr(serving, "SERVING_DB_PATH", dbp)

    # Default config matches everything; cap limits the count.
    from telegram_bot.filters import DEFAULT_USER_CONFIG

    listings = bot._get_latest_listings(dict(DEFAULT_USER_CONFIG), limit=1)
    assert len(listings) == 1


def test_listing_meta_found_and_missing(tmp_path, monkeypatch):
    dbp = tmp_path / "pipeline.duckdb"
    _make_snapshot_db(dbp)
    monkeypatch.setattr(serving, "SERVING_DB_PATH", dbp)

    meta = serving.listing_meta("id1")
    assert meta is not None
    assert meta["title"] == "Senior Dev"
    assert meta["company"] == "Acme"
    assert meta["url"] == "https://justjoin.it/offers/slug-1"

    # An id not in the snapshot (de-listed / aged out) yields None.
    assert serving.listing_meta("gone-uuid") is None


def test_hydrate_tracker_rows_backfills_and_persists(tmp_path, monkeypatch):
    import importlib

    dbp = tmp_path / "pipeline.duckdb"
    _make_snapshot_db(dbp)
    monkeypatch.setattr(serving, "SERVING_DB_PATH", dbp)

    monkeypatch.setenv("TRACKER_DB_PATH", str(tmp_path / "tracker.db"))
    import telegram_bot.tracker as tracker_mod

    tracker = importlib.reload(tracker_mod)
    monkeypatch.setattr(bot, "tracker", tracker)

    # Simulate a row tracked via inline button while metadata was uncached.
    tracker.set_status(7, "id1", "interested")
    rows, _ = tracker.list_page(7, None, 10, 0)
    assert rows[0].get("title") is None

    hydrated = bot._hydrate_tracker_rows(7, rows)
    assert hydrated[0]["title"] == "Senior Dev"
    assert hydrated[0]["url"] == "https://justjoin.it/offers/slug-1"

    # Backfill was persisted to the tracker DB.
    persisted = tracker.list_page(7, None, 10, 0)[0][0]
    assert persisted["title"] == "Senior Dev"
    assert persisted["company"] == "Acme"


def test_hydrate_tracker_rows_leaves_delisted_untouched(tmp_path, monkeypatch):
    import importlib

    dbp = tmp_path / "pipeline.duckdb"
    _make_snapshot_db(dbp)
    monkeypatch.setattr(serving, "SERVING_DB_PATH", dbp)

    monkeypatch.setenv("TRACKER_DB_PATH", str(tmp_path / "tracker.db"))
    import telegram_bot.tracker as tracker_mod

    tracker = importlib.reload(tracker_mod)
    monkeypatch.setattr(bot, "tracker", tracker)

    tracker.set_status(7, "gone-uuid", "rejected")
    rows, _ = tracker.list_page(7, None, 10, 0)
    hydrated = bot._hydrate_tracker_rows(7, rows)
    assert hydrated[0].get("title") is None


def test_build_tracker_message_placeholder_for_missing_title():
    rows = [{"listing_id": "gone-uuid", "status": "rejected", "title": None}]
    text, _ = bot._build_tracker_message(rows, {"rejected": 1}, None, 0, 1)
    assert "gone-uuid" not in text
    assert "Offer no longer available" in text
