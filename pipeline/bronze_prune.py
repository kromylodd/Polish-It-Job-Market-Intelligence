"""
Bronze retention pruning: delete rows older than BRONZE_RETENTION_DAYS.

Called from run_pipeline.py after a successful silver step so that a failed
run never prunes data that hasn't been safely captured downstream yet.

Design decisions:
  * Retention window is env-tunable (BRONZE_RETENTION_DAYS, default 30).
  * DELETE is issued in a single statement — on the trimmed table (≤300k rows,
    ~10k deleted per day) this is well within the e2-micro's 1 GB RAM budget.
    Chunking is only needed for the one-time cleanup of an already-bloated table.
  * VACUUM is run only when rows were actually pruned, since VACUUM on DuckDB
    is a no-op cost when nothing changed, but still takes a moment.
  * Connection is passed in (not opened here) so the caller controls the
    transaction boundary and the function is easy to unit-test.
"""

import logging
import os

import duckdb

from pipeline import SCHEMA_BRONZE

logger = logging.getLogger(__name__)

BRONZE_TABLE = f"{SCHEMA_BRONZE}.raw_job_listings"

# Default retention window in days.  Override via BRONZE_RETENTION_DAYS env var.
_DEFAULT_RETENTION_DAYS = 30


def prune_bronze(con: duckdb.DuckDBPyConnection, retention_days: int | None = None) -> int:
    """Delete bronze rows older than *retention_days* by ingested_at timestamp.

    Args:
        con: Open, writable DuckDB connection to the pipeline database.
        retention_days: Days of history to keep.  Defaults to the value of
            BRONZE_RETENTION_DAYS env var, or 30 if not set.

    Returns:
        Number of rows deleted.
    """
    if retention_days is None:
        try:
            retention_days = int(os.environ.get("BRONZE_RETENTION_DAYS", _DEFAULT_RETENTION_DAYS))
        except ValueError:
            logger.warning(
                "BRONZE_RETENTION_DAYS env var is not a valid integer — using default %d",
                _DEFAULT_RETENTION_DAYS,
            )
            retention_days = _DEFAULT_RETENTION_DAYS

    if retention_days <= 0:
        logger.warning("BRONZE_RETENTION_DAYS=%d is not positive — skipping prune", retention_days)
        return 0

    # DuckDB's INTERVAL syntax does not support ? parameter binding for the
    # quantifier (e.g. "INTERVAL ? DAYS" raises a parser error).  We interpolate
    # the integer directly — it is safe because retention_days has already been
    # validated as a positive int above.
    cutoff_expr = f"current_timestamp - INTERVAL {retention_days} DAYS"

    # Count rows that will be pruned before deleting (cheap predicate scan).
    to_prune = con.execute(
        f"SELECT count(*) FROM {BRONZE_TABLE} WHERE ingested_at < {cutoff_expr}"  # noqa: S608
    ).fetchone()[0]

    if to_prune == 0:
        logger.info(
            "Bronze prune: nothing to delete (all rows within %d-day retention window)",
            retention_days,
        )
        return 0

    con.execute(f"DELETE FROM {BRONZE_TABLE} WHERE ingested_at < {cutoff_expr}")  # noqa: S608
    con.commit()

    # VACUUM only when rows were actually deleted — reclaims storage pages.
    con.execute("VACUUM")

    remaining = con.execute(f"SELECT count(*) FROM {BRONZE_TABLE}").fetchone()[0]  # noqa: S608
    logger.info(
        "Bronze prune: deleted %d rows older than %d days (%d rows remain)",
        to_prune,
        retention_days,
        remaining,
    )
    return to_prune
