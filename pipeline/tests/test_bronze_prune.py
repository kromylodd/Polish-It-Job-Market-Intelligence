"""Tests for pipeline.bronze_prune — retention pruning of the bronze table."""

from datetime import datetime, timedelta, timezone

import duckdb

from pipeline.bronze_prune import _DEFAULT_RETENTION_DAYS, prune_bronze

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_db(tmp_path) -> duckdb.DuckDBPyConnection:
    """Create an in-memory-style temp DuckDB with the bronze table seeded."""
    db_path = str(tmp_path / "test_pipeline.duckdb")
    con = duckdb.connect(db_path)
    con.execute("CREATE SCHEMA IF NOT EXISTS bronze")
    con.execute(
        """
        CREATE TABLE bronze.raw_job_listings (
            listing_id  VARCHAR NOT NULL,
            source_run_id VARCHAR,
            ingested_at TIMESTAMP DEFAULT current_timestamp
        )
        """
    )
    return con


def _insert_rows(con: duckdb.DuckDBPyConnection, ages_days: list[int]) -> None:
    """Insert one row per age, with ingested_at = now - age_days."""
    now = datetime.now(timezone.utc)
    for i, age in enumerate(ages_days):
        ts = (now - timedelta(days=age)).strftime("%Y-%m-%d %H:%M:%S")
        con.execute(
            "INSERT INTO bronze.raw_job_listings (listing_id, source_run_id, ingested_at) "
            "VALUES (?, ?, ?)",
            [f"listing_{i}", f"run_{i}", ts],
        )
    con.commit()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_prune_deletes_old_rows(tmp_path):
    """Rows older than retention_days are deleted; recent rows survive."""
    con = _make_db(tmp_path)
    # 3 rows within window, 2 outside
    _insert_rows(con, ages_days=[1, 5, 29, 31, 60])

    pruned = prune_bronze(con, retention_days=30)

    assert pruned == 2
    remaining = con.execute(
        "SELECT listing_id FROM bronze.raw_job_listings ORDER BY listing_id"
    ).fetchall()
    assert len(remaining) == 3
    # The two old rows (31d and 60d) must be gone
    ids = {r[0] for r in remaining}
    assert "listing_3" not in ids  # 31 days old
    assert "listing_4" not in ids  # 60 days old
    con.close()


def test_prune_nothing_to_delete(tmp_path):
    """Returns 0 and leaves the table untouched when all rows are within window."""
    con = _make_db(tmp_path)
    _insert_rows(con, ages_days=[0, 1, 15, 29])

    pruned = prune_bronze(con, retention_days=30)

    assert pruned == 0
    assert con.execute("SELECT count(*) FROM bronze.raw_job_listings").fetchone()[0] == 4
    con.close()


def test_prune_empty_table(tmp_path):
    """Returns 0 gracefully on an empty bronze table."""
    con = _make_db(tmp_path)
    pruned = prune_bronze(con, retention_days=30)
    assert pruned == 0
    con.close()


def test_prune_all_rows_old(tmp_path):
    """All rows pruned when every row is outside the retention window."""
    con = _make_db(tmp_path)
    _insert_rows(con, ages_days=[31, 45, 90])

    pruned = prune_bronze(con, retention_days=30)

    assert pruned == 3
    assert con.execute("SELECT count(*) FROM bronze.raw_job_listings").fetchone()[0] == 0
    con.close()


def test_prune_retention_days_env(tmp_path, monkeypatch):
    """BRONZE_RETENTION_DAYS env var is respected when retention_days not passed."""
    monkeypatch.setenv("BRONZE_RETENTION_DAYS", "7")
    con = _make_db(tmp_path)
    # Row at 5d (within 7d window), row at 8d (outside)
    _insert_rows(con, ages_days=[5, 8])

    pruned = prune_bronze(con)  # no explicit retention_days — reads env

    assert pruned == 1
    remaining = con.execute("SELECT listing_id FROM bronze.raw_job_listings").fetchall()
    assert len(remaining) == 1
    assert remaining[0][0] == "listing_0"  # 5-day-old row survives
    con.close()


def test_prune_default_retention_is_30(tmp_path, monkeypatch):
    """Default retention window is 30 days when env var is not set."""
    monkeypatch.delenv("BRONZE_RETENTION_DAYS", raising=False)
    assert _DEFAULT_RETENTION_DAYS == 30

    con = _make_db(tmp_path)
    # Use ages that are unambiguously within vs outside the 30-day window.
    # Avoid the exact 30-day boundary — the row is inserted slightly before
    # current_timestamp is evaluated in the DELETE, so timing jitter can push it
    # either side.  Use 28d (clearly inside) and 32d (clearly outside) instead.
    _insert_rows(con, ages_days=[28, 32])

    pruned = prune_bronze(con)

    assert pruned == 1  # only the 32-day-old row is pruned
    remaining = con.execute("SELECT listing_id FROM bronze.raw_job_listings").fetchall()
    assert len(remaining) == 1
    assert remaining[0][0] == "listing_0"  # 28-day-old row survives
    con.close()


def test_prune_invalid_env_falls_back_to_default(tmp_path, monkeypatch):
    """Invalid BRONZE_RETENTION_DAYS falls back to 30-day default without crashing."""
    monkeypatch.setenv("BRONZE_RETENTION_DAYS", "not-a-number")
    con = _make_db(tmp_path)
    _insert_rows(con, ages_days=[1])

    pruned = prune_bronze(con)  # should not raise
    assert pruned == 0  # 1-day-old row is within 30-day default
    con.close()


def test_prune_zero_retention_days_skipped(tmp_path):
    """retention_days=0 is a no-op (logs warning, returns 0)."""
    con = _make_db(tmp_path)
    _insert_rows(con, ages_days=[365])

    pruned = prune_bronze(con, retention_days=0)

    assert pruned == 0
    assert con.execute("SELECT count(*) FROM bronze.raw_job_listings").fetchone()[0] == 1
    con.close()
