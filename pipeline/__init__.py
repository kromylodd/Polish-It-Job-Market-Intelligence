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
