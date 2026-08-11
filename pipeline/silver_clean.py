"""
Silver clean: deduplicate, standardize, remove invalid records.

Replaces Databricks notebook 02_silver_clean.py. Uses DuckDB SQL directly
(the transformations are simple enough that Polars isn't needed here).

Logic preserved from the original:
  1. Deduplicate: keep latest record per listing_id (by date_collected DESC)
  2. Standardize: trim/lowercase category, seniority, workplace_type
  3. Normalize: "office" → "onsite" for workplace_type
  4. Filter: drop rows with null/empty listing_id, title, or company_name
"""

import logging

import duckdb

from pipeline import PIPELINE_DB_PATH, SCHEMA_BRONZE, SCHEMA_SILVER

logger = logging.getLogger(__name__)

BRONZE_TABLE = f"{SCHEMA_BRONZE}.raw_job_listings"
SILVER_TABLE = f"{SCHEMA_SILVER}.stg_listings"


def _ensure_schema(con: duckdb.DuckDBPyConnection) -> None:
    """Create the silver schema if it doesn't exist."""
    con.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA_SILVER}")


def clean() -> int:
    """Run silver clean transformation. Returns row count of the silver table.

    This is a full overwrite (CTAS) of the silver table from the bronze table,
    matching the original Databricks notebook behavior (mode='overwrite').
    """
    con = duckdb.connect(str(PIPELINE_DB_PATH))
    try:
        _ensure_schema(con)

        # Verify bronze table exists and has data
        bronze_count = con.execute(f"SELECT count(*) FROM {BRONZE_TABLE}").fetchone()[0]
        if bronze_count == 0:
            logger.warning("Bronze table is empty — nothing to clean")
            return 0
        logger.info("Bronze table: %d rows", bronze_count)

        # Drop and recreate silver table (full overwrite)
        con.execute(f"DROP TABLE IF EXISTS {SILVER_TABLE}")

        # The transformation:
        # 1. Deduplicate: ROW_NUMBER() partitioned by listing_id, ordered by date_collected DESC
        # 2. Standardize: trim + lower on text fields
        # 3. Normalize: "office" → "onsite"
        # 4. Filter: non-null listing_id, title, company_name
        con.execute(f"""
            CREATE TABLE {SILVER_TABLE} AS
            WITH ranked AS (
                SELECT *,
                    ROW_NUMBER() OVER (
                        PARTITION BY listing_id
                        ORDER BY date_collected DESC
                    ) AS _rn
                FROM {BRONZE_TABLE}
            ),
            deduped AS (
                SELECT * FROM ranked WHERE _rn = 1
            )
            SELECT
                listing_id,
                slug,
                title,
                apply_url,
                apply_method,
                company_name,
                trim(category) AS category,
                lower(trim(seniority)) AS seniority,
                CASE
                    WHEN lower(trim(workplace_type)) = 'office' THEN 'onsite'
                    ELSE lower(trim(workplace_type))
                END AS workplace_type,
                working_time,
                cities,
                salary_variants,
                required_skills,
                nice_to_have_skills,
                description,
                posted_date,
                last_published_date,
                expiry_date,
                is_promoted,
                is_super_offer,
                is_remote_interview,
                date_collected,
                source_run_id,
                source_file,
                ingested_at,
                current_timestamp AS silver_loaded_at
            FROM deduped
            WHERE listing_id IS NOT NULL
              AND title IS NOT NULL AND title != ''
              AND company_name IS NOT NULL AND company_name != ''
        """)
        con.commit()

        silver_count = con.execute(f"SELECT count(*) FROM {SILVER_TABLE}").fetchone()[0]
        logger.info(
            "Silver table: %d rows (deduped from %d bronze rows, %d removed)",
            silver_count,
            bronze_count,
            bronze_count - silver_count,
        )
        return silver_count
    finally:
        con.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    count = clean()
    print(f"Silver table: {count} rows")
