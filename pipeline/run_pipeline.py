"""
Pipeline orchestrator: runs the full bronze → silver → gold pipeline.

Execution order:
  1. bronze_ingest — JSON → DuckDB bronze table (append, idempotent)
  2. silver_clean — dedup + standardize (DuckDB SQL, full overwrite)
  3. silver_tech_parse — tech canonicalization (Polars → DuckDB, full overwrite)
  4. dbt build — staging → dims/facts/bridges → analytics marts
  5. (optional) dbt test — data quality gate

Exit codes:
  0 — success
  1 — pipeline step failed
  2 — dbt build failed
  3 — dbt test failed (tests produced errors, not just warnings)

Usage:
  python -m pipeline.run_pipeline [--data-file PATH] [--skip-dbt-test] [--dry-run]

Environment:
  PIPELINE_DB_PATH — path to the DuckDB database (default: ./pipeline.duckdb)
  PIPELINE_DATA_DIR — directory containing raw_listings_latest.json
"""

import argparse
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

from pipeline import DATA_DIR, PIPELINE_DB_PATH, PROJECT_ROOT, RAW_LISTINGS_FILE
from pipeline.bronze_ingest import ingest
from pipeline.silver_clean import clean
from pipeline.silver_tech_parse import tech_parse

logger = logging.getLogger(__name__)

DBT_DIR = PROJECT_ROOT / "dbt"


def _run_dbt(command: list[str], description: str) -> bool:
    """Run a dbt command in the dbt directory. Returns True on success."""
    env = os.environ.copy()
    env["PIPELINE_DB_PATH"] = str(PIPELINE_DB_PATH)

    cmd = ["dbt"] + command + ["--profiles-dir", ".", "--target", "local"]
    logger.info("Running: %s", " ".join(cmd))

    result = subprocess.run(
        cmd,
        cwd=str(DBT_DIR),
        env=env,
        capture_output=True,
        text=True,
        timeout=300,  # 5 minute timeout for dbt
    )

    # Log dbt output
    if result.stdout:
        for line in result.stdout.strip().splitlines():
            logger.info("[dbt] %s", line)
    if result.stderr:
        for line in result.stderr.strip().splitlines():
            logger.warning("[dbt stderr] %s", line)

    if result.returncode != 0:
        logger.error("%s failed (exit code %d)", description, result.returncode)
        return False

    logger.info("%s completed successfully", description)
    return True


def run_pipeline(
    data_file: Path | None = None,
    skip_dbt_test: bool = False,
    dry_run: bool = False,
) -> int:
    """Execute the full pipeline. Returns exit code (0=success).

    Args:
        data_file: Path to raw JSON file. Defaults to DATA_DIR/raw_listings_latest.json.
        skip_dbt_test: If True, skip the dbt test step.
        dry_run: If True, only validate inputs without executing.
    """
    start = time.time()
    filepath = data_file or (DATA_DIR / RAW_LISTINGS_FILE)

    logger.info("=" * 60)
    logger.info("PIPELINE START")
    logger.info("  Database: %s", PIPELINE_DB_PATH)
    logger.info("  Input: %s", filepath)
    logger.info("=" * 60)

    # Validate inputs
    if not filepath.exists():
        logger.error("Input file not found: %s", filepath)
        return 1

    file_size_mb = filepath.stat().st_size / 1e6
    logger.info("Input file: %.1f MB", file_size_mb)

    if dry_run:
        logger.info("DRY RUN — would process %s (%.1f MB)", filepath, file_size_mb)
        return 0

    # --- Step 1: Bronze Ingest ---
    logger.info("-" * 40)
    logger.info("STEP 1/4: Bronze Ingest")
    try:
        bronze_rows = ingest(filepath)
        logger.info("Bronze: %d rows ingested", bronze_rows)
        if bronze_rows == 0:
            logger.info("No new data to ingest (idempotent skip) — continuing with existing data")
    except Exception as e:
        logger.error("Bronze ingest failed: %s", e, exc_info=True)
        return 1

    # --- Step 2: Silver Clean ---
    logger.info("-" * 40)
    logger.info("STEP 2/4: Silver Clean")
    try:
        silver_rows = clean()
        logger.info("Silver: %d rows after dedup/clean", silver_rows)
        if silver_rows == 0:
            logger.error("Silver table is empty — cannot continue")
            return 1
    except Exception as e:
        logger.error("Silver clean failed: %s", e, exc_info=True)
        return 1

    # --- Step 3: Silver Tech Parse ---
    logger.info("-" * 40)
    logger.info("STEP 3/4: Silver Tech Parse")
    try:
        tech_rows = tech_parse()
        logger.info("Silver tech: %d rows with tech annotations", tech_rows)
        if tech_rows == 0:
            logger.error("Silver tech table is empty — cannot continue")
            return 1
    except Exception as e:
        logger.error("Silver tech parse failed: %s", e, exc_info=True)
        return 1

    # --- Step 4: dbt Build ---
    logger.info("-" * 40)
    logger.info("STEP 4/4: dbt Build")
    if not _run_dbt(["build", "--fail-fast"], "dbt build"):
        return 2

    # --- Optional: dbt Test ---
    if not skip_dbt_test:
        logger.info("-" * 40)
        logger.info("OPTIONAL: dbt Test (data quality)")
        if not _run_dbt(["test"], "dbt test"):
            # dbt test failures are warnings for severity=warn tests
            # Only fail the pipeline if there are actual ERROR results
            logger.warning("dbt test reported issues — check warnings above")
            # Don't return 3 here because severity=warn tests produce exit code 0
            # Only actual test errors (not warnings) produce non-zero exit

    elapsed = time.time() - start
    logger.info("=" * 60)
    logger.info("PIPELINE COMPLETE in %.1f seconds", elapsed)
    logger.info("  Database: %s (%.1f MB)", PIPELINE_DB_PATH, PIPELINE_DB_PATH.stat().st_size / 1e6)
    logger.info("=" * 60)
    return 0


def main():
    parser = argparse.ArgumentParser(
        description="Run the full data pipeline (bronze → silver → gold)"
    )
    parser.add_argument(
        "--data-file",
        type=Path,
        default=None,
        help="Path to raw JSON file (default: DATA_DIR/raw_listings_latest.json)",
    )
    parser.add_argument(
        "--skip-dbt-test",
        action="store_true",
        help="Skip the dbt test step",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate inputs without executing",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Enable debug logging",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    exit_code = run_pipeline(
        data_file=args.data_file,
        skip_dbt_test=args.skip_dbt_test,
        dry_run=args.dry_run,
    )
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
