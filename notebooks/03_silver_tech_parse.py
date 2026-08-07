# Databricks notebook source
# MAGIC %md
# MAGIC # Silver Tech Parse
# MAGIC Regex/NLP tech-stack + skill canonicalization.
# MAGIC
# MAGIC Two sources of technology data:
# MAGIC 1. **Structured tags** (`required_skills`, `nice_to_have_skills`) — canonicalize variant spellings
# MAGIC 2. **Free-text description** — supplementary extraction via word-boundary regex
# MAGIC
# MAGIC Both mapped to canonical names via the `technology_lookup` dictionary.

# COMMAND ----------

from pyspark.sql import SparkSession
from pyspark.sql.functions import (
    array_distinct,
    array_union,
    col,
    current_timestamp,
    udf,
)
from pyspark.sql.types import ArrayType, StringType

spark = SparkSession.builder.getOrCreate()

# COMMAND ----------

# Configuration
SILVER_TABLE = "job_market.silver.stg_listings"
SILVER_TECH_TABLE = "job_market.silver.listings_with_tech"

# COMMAND ----------

# MAGIC %md
# MAGIC ## Technology Lookup Dictionary
# MAGIC Maps variant spellings → canonical name.
# MAGIC
# MAGIC In production, this is maintained as a dbt seed (`technology_lookup.csv`)
# MAGIC and loaded from the gold schema. For the PySpark step, we keep a Python dict
# MAGIC that mirrors that seed — single source of truth pattern.

# COMMAND ----------

# Technology canonicalization mapping
# Key: lowercase variant → Value: canonical name
# This should stay in sync with dbt/seeds/technology_lookup.csv
TECH_ALIASES: dict[str, str] = {
    # Python ecosystem
    "python": "Python",
    "python3": "Python",
    "py": "Python",
    # JavaScript / TypeScript
    "javascript": "JavaScript",
    "js": "JavaScript",
    "typescript": "TypeScript",
    "ts": "TypeScript",
    # React
    "react": "React",
    "react.js": "React",
    "reactjs": "React",
    # Node
    "node": "Node.js",
    "node.js": "Node.js",
    "nodejs": "Node.js",
    # SQL
    "sql": "SQL",
    "t-sql": "SQL",
    "pl/sql": "SQL",
    "plsql": "SQL",
    # Spark
    "spark": "Apache Spark",
    "apache spark": "Apache Spark",
    "pyspark": "Apache Spark",
    # Airflow
    "airflow": "Apache Airflow",
    "apache airflow": "Apache Airflow",
    # Kafka
    "kafka": "Apache Kafka",
    "apache kafka": "Apache Kafka",
    # dbt
    "dbt": "dbt",
    "data build tool": "dbt",
    # Cloud
    "aws": "AWS",
    "amazon web services": "AWS",
    "amazon-web-services": "AWS",
    "gcp": "GCP",
    "google cloud": "GCP",
    "google cloud platform": "GCP",
    "azure": "Azure",
    "microsoft azure": "Azure",
    # Databases
    "postgresql": "PostgreSQL",
    "postgres": "PostgreSQL",
    "mysql": "MySQL",
    "mongodb": "MongoDB",
    "mongo": "MongoDB",
    "redis": "Redis",
    # Containers
    "docker": "Docker",
    "kubernetes": "Kubernetes",
    "k8s": "Kubernetes",
    # Terraform
    "terraform": "Terraform",
    "tf": "Terraform",
    # Git
    "git": "Git",
    "github": "GitHub",
    "gitlab": "GitLab",
    # Java/Scala
    "java": "Java",
    "scala": "Scala",
    # Go
    "go": "Go",
    "golang": "Go",
    # Databricks
    "databricks": "Databricks",
    # Power BI
    "power bi": "Power BI",
    "powerbi": "Power BI",
    # Tableau
    "tableau": "Tableau",
    # CI/CD
    "ci/cd": "CI/CD",
    "cicd": "CI/CD",
    # Linux
    "linux": "Linux",
}

# Build a set of canonical names for regex matching against descriptions
CANONICAL_TECHS = set(TECH_ALIASES.values())

# COMMAND ----------

# MAGIC %md
# MAGIC ## Canonicalization UDF
# MAGIC Maps structured skill tags to canonical names.

# COMMAND ----------

# Broadcast the alias dict for UDF access
broadcast_aliases = spark.sparkContext.broadcast(TECH_ALIASES)


@udf(returnType=ArrayType(StringType()))
def canonicalize_skills(skills: list[str]) -> list[str]:
    """Map skill tag variants to canonical names."""
    if not skills:
        return []

    aliases = broadcast_aliases.value
    canonical = []

    for skill in skills:
        normalized = skill.strip().lower()
        canonical_name = aliases.get(normalized)
        if canonical_name:
            canonical.append(canonical_name)
        else:
            # Keep the original (title-cased) if not in our lookup
            canonical.append(skill.strip())

    return list(set(canonical))  # dedupe

# COMMAND ----------

# MAGIC %md
# MAGIC ## Description Tech Extraction UDF
# MAGIC Supplementary extraction from free-text — finds technologies mentioned
# MAGIC in the description body that aren't in the structured tags.

# COMMAND ----------

import re

# Broadcast canonical tech list for description scanning
broadcast_canonical = spark.sparkContext.broadcast(list(CANONICAL_TECHS))

# Pre-compile regex patterns for each canonical tech
# Use word boundaries to avoid false positives (e.g., "Go" inside "Google")
TECH_PATTERNS: dict[str, re.Pattern] = {}
for tech in CANONICAL_TECHS:
    # Escape special regex chars in tech names
    escaped = re.escape(tech)
    TECH_PATTERNS[tech] = re.compile(rf"\b{escaped}\b", re.IGNORECASE)

broadcast_patterns = spark.sparkContext.broadcast(
    {k: v.pattern for k, v in TECH_PATTERNS.items()}
)


@udf(returnType=ArrayType(StringType()))
def extract_techs_from_description(description: str) -> list[str]:
    """Extract technology mentions from free-text description using regex."""
    if not description:
        return []

    patterns = broadcast_patterns.value
    found = []

    for tech_name, pattern_str in patterns.items():
        pattern = re.compile(pattern_str, re.IGNORECASE)
        if pattern.search(description):
            found.append(tech_name)

    return found

# COMMAND ----------

# MAGIC %md
# MAGIC ## Apply Canonicalization & Extraction

# COMMAND ----------

silver_df = spark.table(SILVER_TABLE)

tech_df = (
    silver_df
    # Canonicalize structured skills
    .withColumn("canonical_required_skills", canonicalize_skills(col("required_skills")))
    .withColumn("canonical_nice_to_have_skills", canonicalize_skills(col("nice_to_have_skills")))
    # Extract additional techs from description
    .withColumn("description_techs", extract_techs_from_description(col("description")))
    # Merge all technologies into a unified list
    .withColumn(
        "all_technologies",
        array_distinct(
            array_union(
                array_union(col("canonical_required_skills"), col("canonical_nice_to_have_skills")),
                col("description_techs"),
            )
        ),
    )
    .withColumn("tech_parsed_at", current_timestamp())
)

# COMMAND ----------

# Write enriched silver table
(
    tech_df.write
    .format("delta")
    .mode("overwrite")
    .option("overwriteSchema", "true")
    .saveAsTable(SILVER_TECH_TABLE)
)

count = spark.table(SILVER_TECH_TABLE).count()
print(f"Silver tech-parsed table '{SILVER_TECH_TABLE}' now has {count} rows")
