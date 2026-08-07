# Databricks notebook source
# Silver tech parse: canonicalize skill tags + extract techs from description.

import re

from pyspark.sql import SparkSession
from pyspark.sql.functions import array_distinct, array_union, col, current_timestamp, udf
from pyspark.sql.types import ArrayType, StringType

spark = SparkSession.builder.getOrCreate()

SILVER_TABLE = "job_market.silver.stg_listings"
SILVER_TECH_TABLE = "job_market.silver.listings_with_tech"

# Variant -> canonical mapping. Kept in sync with dbt/seeds/technology_lookup.csv.
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
TECH_PATTERNS = {
    tech: re.compile(rf"\b{re.escape(tech)}\b", re.IGNORECASE) for tech in CANONICAL_TECHS
}

broadcast_aliases = spark.sparkContext.broadcast(TECH_ALIASES)
broadcast_patterns = spark.sparkContext.broadcast({k: v.pattern for k, v in TECH_PATTERNS.items()})


@udf(returnType=ArrayType(StringType()))
def canonicalize_skills(skills: list[str]) -> list[str]:
    if not skills:
        return []
    aliases = broadcast_aliases.value
    result = set()
    for s in skills:
        canonical = aliases.get(s.strip().lower())
        result.add(canonical if canonical else s.strip())
    return list(result)


@udf(returnType=ArrayType(StringType()))
def extract_techs_from_description(description: str) -> list[str]:
    if not description:
        return []
    patterns = broadcast_patterns.value
    return [name for name, pat in patterns.items() if re.search(pat, description, re.IGNORECASE)]


# Apply
silver_df = spark.table(SILVER_TABLE)

tech_df = (
    silver_df.withColumn("canonical_required_skills", canonicalize_skills(col("required_skills")))
    .withColumn("canonical_nice_to_have_skills", canonicalize_skills(col("nice_to_have_skills")))
    .withColumn("description_techs", extract_techs_from_description(col("description")))
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

(
    tech_df.write.format("delta")
    .mode("overwrite")
    .option("overwriteSchema", "true")
    .saveAsTable(SILVER_TECH_TABLE)
)

print(f"Silver tech table: {spark.table(SILVER_TECH_TABLE).count()} rows")
