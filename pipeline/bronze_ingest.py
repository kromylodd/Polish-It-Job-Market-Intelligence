"""
Bronze ingest: read raw JSON file → append to DuckDB bronze table.

Replaces the Databricks notebook 01_bronze_ingest.py which used Spark Auto Loader.
Uses Polars for JSON parsing (fast, low-memory) and DuckDB for storage.

Idempotency: uses source_run_id to detect and skip already-ingested runs.
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import polars as pl

from pipeline import DATA_DIR, PIPELINE_DB_PATH, RAW_LISTINGS_FILE, SCHEMA_BRONZE

logger = logging.getLogger(__name__)

BRONZE_TABLE = f"{SCHEMA_BRONZE}.raw_job_listings"


def _ensure_schema(con: duckdb.DuckDBPyConnection) -> None:
    """Create the bronze schema and table if they don't exist."""
    con.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA_BRONZE}")
    con.execute(f"""
        CREATE TABLE IF NOT EXISTS {BRONZE_TABLE} (
            listing_id VARCHAR NOT NULL,
            slug VARCHAR,
            title VARCHAR,
            apply_url VARCHAR,
            apply_method VARCHAR,
            company_name VARCHAR,
            category VARCHAR,
            seniority VARCHAR,
            workplace_type VARCHAR,
            working_time VARCHAR,
            cities VARCHAR[],
            salary_variants JSON,
            required_skills VARCHAR[],
            nice_to_have_skills VARCHAR[],
            description VARCHAR,
            posted_date VARCHAR,
            last_published_date VARCHAR,
            expiry_date VARCHAR,
            is_promoted BOOLEAN,
            is_super_offer BOOLEAN,
            is_remote_interview BOOLEAN,
            date_collected VARCHAR,
            source_run_id VARCHAR,
            source_file VARCHAR,
            ingested_at TIMESTAMP DEFAULT current_timestamp
        )
    """)


def _already_ingested(con: duckdb.DuckDBPyConnection, run_id: str) -> bool:
    """Check if a given source_run_id was already ingested (idempotency guard)."""
    result = con.execute(
        f"SELECT 1 FROM {BRONZE_TABLE} WHERE source_run_id = ? LIMIT 1",
        [run_id],
    ).fetchone()
    return result is not None


def _load_json(filepath: Path) -> tuple[dict, list[dict]]:
    """Load and validate the raw JSON file. Returns (metadata, listings)."""
    with open(filepath, "r", encoding="utf-8") as f:
        data = json.load(f)

    if not isinstance(data, dict):
        raise ValueError(f"Expected JSON object at top level, got {type(data).__name__}")

    metadata = data.get("metadata", {})
    listings = data.get("listings", [])

    if not isinstance(listings, list):
        raise ValueError(f"Expected 'listings' to be a list, got {type(listings).__name__}")

    if not listings:
        logger.warning("JSON file contains zero listings")

    return metadata, listings


def _listings_to_polars(listings: list[dict], source_file: str) -> pl.DataFrame:
    """Convert raw listing dicts to a Polars DataFrame matching the bronze schema.

    Handles type coercion defensively — malformed fields become null rather than
    crashing the entire ingest.
    """
    rows = []
    for listing in listings:
        # salary_variants: store as JSON string for DuckDB JSON column
        salary_variants = listing.get("salary_variants", [])
        if not isinstance(salary_variants, list):
            salary_variants = []

        # cities: ensure list of strings
        cities = listing.get("cities", [])
        if not isinstance(cities, list):
            cities = [str(cities)] if cities else []

        # skills: ensure list of strings
        required_skills = listing.get("required_skills", [])
        if not isinstance(required_skills, list):
            required_skills = []

        nice_to_have = listing.get("nice_to_have_skills", [])
        if not isinstance(nice_to_have, list):
            nice_to_have = []

        rows.append(
            {
                "listing_id": str(listing.get("listing_id", "")),
                "slug": str(listing.get("slug", "") or ""),
                "title": str(listing.get("title", "") or ""),
                "apply_url": str(listing.get("apply_url", "") or ""),
                "apply_method": str(listing.get("apply_method", "") or ""),
                "company_name": str(listing.get("company_name", "") or ""),
                "category": str(listing.get("category", "") or ""),
                "seniority": str(listing.get("seniority", "") or ""),
                "workplace_type": str(listing.get("workplace_type", "") or ""),
                "working_time": str(listing.get("working_time", "") or ""),
                "cities": [str(c) for c in cities],
                "salary_variants": json.dumps(salary_variants),
                "required_skills": [str(s) for s in required_skills if s],
                "nice_to_have_skills": [str(s) for s in nice_to_have if s],
                "description": str(listing.get("description", "") or ""),
                "posted_date": str(listing.get("posted_date", "") or ""),
                "last_published_date": str(listing.get("last_published_date", "") or ""),
                "expiry_date": str(listing.get("expiry_date", "") or ""),
                "is_promoted": bool(listing.get("is_promoted", False)),
                "is_super_offer": bool(listing.get("is_super_offer", False)),
                "is_remote_interview": bool(listing.get("is_remote_interview", False)),
                "date_collected": str(listing.get("date_collected", "") or ""),
                "source_run_id": str(listing.get("source_run_id", "") or ""),
                "source_file": source_file,
            }
        )

    return pl.DataFrame(rows)


def ingest(filepath: Path | None = None) -> int:
    """Run bronze ingest. Returns number of rows ingested.

    Args:
        filepath: Path to raw JSON file. Defaults to DATA_DIR/raw_listings_latest.json.

    Returns:
        Number of new rows appended to the bronze table.

    Raises:
        FileNotFoundError: if the input file doesn't exist.
        ValueError: if the JSON structure is invalid.
    """
    if filepath is None:
        filepath = DATA_DIR / RAW_LISTINGS_FILE

    if not filepath.exists():
        raise FileNotFoundError(f"Input file not found: {filepath}")

    logger.info("Bronze ingest: reading %s", filepath)
    metadata, listings = _load_json(filepath)

    if not listings:
        logger.info("No listings to ingest")
        return 0

    # Determine run_id from the first listing (all share the same source_run_id)
    run_id = listings[0].get("source_run_id", "")
    if not run_id:
        # Fallback: use date_collected as a pseudo run_id
        run_id = metadata.get("date_collected", datetime.now(timezone.utc).isoformat())

    # Connect to DuckDB (WAL mode for concurrent read access by the bot)
    PIPELINE_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(PIPELINE_DB_PATH))
    try:
        con.execute("PRAGMA enable_progress_bar")
        # WAL mode allows readers (bot) to not block on writes (pipeline)
        con.execute("SET wal_autocheckpoint = '512MB'")

        _ensure_schema(con)

        # Idempotency: skip if this run was already ingested
        if _already_ingested(con, run_id):
            logger.info("Run %s already ingested — skipping (idempotent)", run_id)
            return 0

        # Transform to Polars DataFrame
        df = _listings_to_polars(listings, str(filepath.name))
        logger.info("Parsed %d listings into DataFrame", len(df))

        # Insert into DuckDB — explicit column list to avoid ordering issues.
        # The DataFrame has all columns except ingested_at (which gets DEFAULT).
        cols = [
            "listing_id",
            "slug",
            "title",
            "apply_url",
            "apply_method",
            "company_name",
            "category",
            "seniority",
            "workplace_type",
            "working_time",
            "cities",
            "salary_variants",
            "required_skills",
            "nice_to_have_skills",
            "description",
            "posted_date",
            "last_published_date",
            "expiry_date",
            "is_promoted",
            "is_super_offer",
            "is_remote_interview",
            "date_collected",
            "source_run_id",
            "source_file",
        ]
        col_list = ", ".join(cols)
        con.execute(
            f"INSERT INTO {BRONZE_TABLE} ({col_list}, ingested_at) "
            f"SELECT {col_list}, current_timestamp FROM df"
        )
        con.commit()

        count = con.execute(f"SELECT count(*) FROM {BRONZE_TABLE}").fetchone()[0]
        logger.info("Bronze table: %d total rows (+%d new)", count, len(df))
        return len(df)
    finally:
        con.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    rows = ingest()
    print(f"Ingested {rows} rows")
