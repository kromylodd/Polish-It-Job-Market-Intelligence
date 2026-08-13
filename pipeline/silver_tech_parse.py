"""
Silver tech parse: canonicalize skill tags + extract techs from description.

Replaces Databricks notebook 03_silver_tech_parse.py. Uses Polars for the
regex/mapping operations (same UDF logic, but native Python instead of PySpark).

Output: silver.listings_with_tech — the table that dbt reads as its source.
Adds columns: canonical_required_skills, canonical_nice_to_have_skills,
description_techs, all_technologies, tech_parsed_at.
"""

import logging
import re
from datetime import datetime, timezone

import duckdb
import polars as pl

from pipeline import PIPELINE_DB_PATH, SCHEMA_SILVER

logger = logging.getLogger(__name__)

INPUT_TABLE = f"{SCHEMA_SILVER}.cleaned_listings"
OUTPUT_TABLE = f"{SCHEMA_SILVER}.listings_with_tech"

# Variant -> canonical mapping. Kept in sync with dbt/seeds/technology_lookup.csv
# and the original Databricks notebook.
TECH_ALIASES: dict[str, str] = {
    "python": "Python",
    "python3": "Python",
    "py": "Python",
    "javascript": "JavaScript",
    "js": "JavaScript",
    "typescript": "TypeScript",
    "ts": "TypeScript",
    "react": "React",
    "react.js": "React",
    "reactjs": "React",
    "node": "Node.js",
    "node.js": "Node.js",
    "nodejs": "Node.js",
    "sql": "SQL",
    "t-sql": "SQL",
    "pl/sql": "SQL",
    "spark": "Apache Spark",
    "apache spark": "Apache Spark",
    "pyspark": "Apache Spark",
    "airflow": "Apache Airflow",
    "apache airflow": "Apache Airflow",
    "kafka": "Apache Kafka",
    "apache kafka": "Apache Kafka",
    "dbt": "dbt",
    "data build tool": "dbt",
    "aws": "AWS",
    "amazon web services": "AWS",
    "gcp": "GCP",
    "google cloud": "GCP",
    "google cloud platform": "GCP",
    "azure": "Azure",
    "microsoft azure": "Azure",
    "postgresql": "PostgreSQL",
    "postgres": "PostgreSQL",
    "mysql": "MySQL",
    "mongodb": "MongoDB",
    "mongo": "MongoDB",
    "redis": "Redis",
    "docker": "Docker",
    "kubernetes": "Kubernetes",
    "k8s": "Kubernetes",
    "terraform": "Terraform",
    "git": "Git",
    "java": "Java",
    "scala": "Scala",
    "go": "Go",
    "golang": "Go",
    "databricks": "Databricks",
    "power bi": "Power BI",
    "powerbi": "Power BI",
    "tableau": "Tableau",
    "linux": "Linux",
}

CANONICAL_TECHS = set(TECH_ALIASES.values())
TECH_PATTERNS: dict[str, re.Pattern] = {
    tech: re.compile(rf"\b{re.escape(tech)}\b", re.IGNORECASE) for tech in CANONICAL_TECHS
}


def canonicalize_skills(skills) -> list[str]:
    """Map skill names to their canonical form.

    Note: Polars map_elements passes a pl.Series for List columns, not a Python list.
    """
    if skills is None:
        return []
    # Convert pl.Series to Python list if needed
    if hasattr(skills, "to_list"):
        skills = skills.to_list()
    if not skills:
        return []
    result = set()
    for s in skills:
        if not s:
            continue
        canonical = TECH_ALIASES.get(s.strip().lower())
        result.add(canonical if canonical else s.strip())
    return sorted(result)  # sorted for deterministic output


def extract_techs_from_description(description: str | None) -> list[str]:
    """Extract known technology names from free-text description using regex."""
    if not description or not isinstance(description, str):
        return []
    return sorted(name for name, pat in TECH_PATTERNS.items() if pat.search(description))


def _merge_unique(a, b, c) -> list[str]:
    """Merge three lists into a sorted deduplicated list.

    Handles pl.Series, Python lists, and None values.
    """

    def _to_list(x):
        if x is None:
            return []
        if hasattr(x, "to_list"):
            return x.to_list()
        if isinstance(x, list):
            return x
        return list(x) if hasattr(x, "__iter__") else []

    return sorted(set(_to_list(a)) | set(_to_list(b)) | set(_to_list(c)))


def tech_parse() -> int:
    """Run tech parse transformation. Returns row count of output table.

    Reads from silver.cleaned_listings, applies tech canonicalization and
    description extraction, writes to silver.listings_with_tech.
    """
    con = duckdb.connect(str(PIPELINE_DB_PATH))
    try:
        # Verify input table exists
        input_count = con.execute(f"SELECT count(*) FROM {INPUT_TABLE}").fetchone()[0]
        if input_count == 0:
            logger.warning("Input table %s is empty — nothing to parse", INPUT_TABLE)
            return 0
        logger.info("Input table %s: %d rows", INPUT_TABLE, input_count)

        # Read into Polars for processing
        # DuckDB arrays come through as Python lists in the fetched rows
        df = con.execute(f"SELECT * FROM {INPUT_TABLE}").pl()
        logger.info("Loaded %d rows into Polars", len(df))

        # Apply canonicalization using map_elements (applies Python function per-row)
        df = df.with_columns(
            [
                pl.col("required_skills")
                .map_elements(canonicalize_skills, return_dtype=pl.List(pl.Utf8))
                .alias("canonical_required_skills"),
                pl.col("nice_to_have_skills")
                .map_elements(canonicalize_skills, return_dtype=pl.List(pl.Utf8))
                .alias("canonical_nice_to_have_skills"),
                pl.col("description")
                .map_elements(extract_techs_from_description, return_dtype=pl.List(pl.Utf8))
                .alias("description_techs"),
            ]
        )

        # Merge all technology sources into all_technologies
        df = df.with_columns(
            pl.struct(
                ["canonical_required_skills", "canonical_nice_to_have_skills", "description_techs"]
            )
            .map_elements(
                lambda row: _merge_unique(
                    row.get("canonical_required_skills"),
                    row.get("canonical_nice_to_have_skills"),
                    row.get("description_techs"),
                ),
                return_dtype=pl.List(pl.Utf8),
            )
            .alias("all_technologies")
        )

        # Add tech_parsed_at timestamp
        now = datetime.now(timezone.utc)
        df = df.with_columns(pl.lit(now).alias("tech_parsed_at"))

        # Write to DuckDB (full overwrite)
        con.execute(f"DROP TABLE IF EXISTS {OUTPUT_TABLE}")
        con.execute(f"CREATE TABLE {OUTPUT_TABLE} AS SELECT * FROM df")
        con.commit()

        output_count = con.execute(f"SELECT count(*) FROM {OUTPUT_TABLE}").fetchone()[0]
        logger.info("Output table %s: %d rows", OUTPUT_TABLE, output_count)
        return output_count
    finally:
        con.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    count = tech_parse()
    print(f"Silver tech table: {count} rows")
