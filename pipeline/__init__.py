"""
Local pipeline module — replaces Databricks notebooks.

Processes raw JSON listings through bronze → silver → gold layers using
Polars for data processing and DuckDB as the analytical database.
All output lands in a single DuckDB file that the Telegram bot reads directly.
"""

import os
from pathlib import Path

# Root directory of the project
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Pipeline database — the single analytical store.
# The bot's serving layer reads from this file directly (read-only).
# WAL mode allows concurrent reads while the pipeline writes.
PIPELINE_DB_PATH = Path(
    os.environ.get(
        "PIPELINE_DB_PATH",
        str(PROJECT_ROOT / "pipeline.duckdb"),
    )
)

# Input data directory (scraper output lands here via SCP from GitHub Actions)
DATA_DIR = Path(
    os.environ.get(
        "PIPELINE_DATA_DIR",
        str(PROJECT_ROOT / "data"),
    )
)

# Fixed input filename (matches scraper output)
RAW_LISTINGS_FILE = "raw_listings_latest.json"

# DuckDB schema names (matching the existing medallion architecture)
SCHEMA_BRONZE = "bronze"
SCHEMA_SILVER = "silver"
SCHEMA_GOLD = "gold"

# DuckDB memory / temp-dir settings
# ─────────────────────────────────
# The silver-clean step runs a ROW_NUMBER() window function over the entire
# bronze table.  As the bronze table grows (310 k+ rows and rising) DuckDB
# can exhaust the VM's RAM and get OOM-killed (exit 137) unless we:
#   1. Cap memory so DuckDB spills to disk instead of crashing, and
#   2. Point the spill directory at a writable, sufficiently-large path.
#
# DUCKDB_MEMORY_LIMIT  — any value accepted by DuckDB's SET memory_limit pragma
#                        (e.g. "1GB", "512MB").  Defaults to "1GB".
# DUCKDB_TEMP_DIR      — directory for on-disk spill files.
#                        Defaults to /tmp/duckdb_tmp (created automatically).
DUCKDB_MEMORY_LIMIT: str = os.environ.get("DUCKDB_MEMORY_LIMIT", "512MB")
DUCKDB_TEMP_DIR: str = os.environ.get("DUCKDB_TEMP_DIR", "/tmp/duckdb_tmp")


def configure_duckdb(con) -> None:  # type: ignore[no-untyped-def]
    """Apply memory limit and temp-dir settings to an open DuckDB connection.

    Call this immediately after duckdb.connect() in any pipeline step that
    runs large queries (silver_clean, silver_tech_parse, bronze_prune, etc.).
    """

    # Create the temp dir if it doesn't exist
    Path(DUCKDB_TEMP_DIR).mkdir(parents=True, exist_ok=True)

    con.execute(f"SET memory_limit='{DUCKDB_MEMORY_LIMIT}'")
    con.execute(f"SET temp_directory='{DUCKDB_TEMP_DIR}'")
