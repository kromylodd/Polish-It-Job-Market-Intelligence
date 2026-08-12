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
