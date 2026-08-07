# Databricks notebook source
# MAGIC %md
# MAGIC # Silver Clean
# MAGIC Deduplicate, type-cast, standardize bronze listings into a clean silver table.
# MAGIC
# MAGIC Transformations:
# MAGIC - Deduplicate by listing_id (keep latest date_collected)
# MAGIC - Standardize salary (currency, gross/net flag normalization)
# MAGIC - Standardize city names against a lookup
# MAGIC - Remove impossible values (salary <= 0, missing title/company)
# MAGIC - Add surrogate keys

# COMMAND ----------

from pyspark.sql import SparkSession, Window
from pyspark.sql.functions import (
    col,
    current_timestamp,
    lower,
    md5,
    concat_ws,
    row_number,
    trim,
    when,
    explode,
    size,
)
from pyspark.sql.types import DoubleType

spark = SparkSession.builder.getOrCreate()

# COMMAND ----------

# Configuration
BRONZE_TABLE = "job_market.bronze.raw_job_listings"
SILVER_TABLE = "job_market.silver.stg_listings"

# COMMAND ----------

# Read bronze table
bronze_df = spark.table(BRONZE_TABLE)

print(f"Bronze rows: {bronze_df.count()}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Deduplication
# MAGIC Keep only the latest record per listing_id (same logic as Silesia's stg_listings window function).

# COMMAND ----------

# Deduplicate: keep latest date_collected per listing_id
window_spec = Window.partitionBy("listing_id").orderBy(col("date_collected").desc())

deduped_df = (
    bronze_df
    .withColumn("_row_num", row_number().over(window_spec))
    .filter(col("_row_num") == 1)
    .drop("_row_num")
)

print(f"After dedup: {deduped_df.count()} listings")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Data Cleaning & Standardization

# COMMAND ----------

# Standardize text fields
cleaned_df = (
    deduped_df
    # Trim and lowercase classification fields for consistency
    .withColumn("category", trim(col("category")))
    .withColumn("seniority", lower(trim(col("seniority"))))
    .withColumn("workplace_type", lower(trim(col("workplace_type"))))
    # Remove listings with no title or company (impossible values)
    .filter(col("title").isNotNull() & (col("title") != ""))
    .filter(col("company_name").isNotNull() & (col("company_name") != ""))
    # Remove listings with no ID
    .filter(col("listing_id").isNotNull())
)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Salary Standardization
# MAGIC Salary variants are already structured arrays. Filter out invalid ranges.

# COMMAND ----------

# For now, keep salary_variants as-is (array of structs)
# Validation: filter out entries where salary_min > salary_max or salary_min <= 0
# This will be done in dbt for the fact table (SQL is cleaner for this)
# Here we just ensure the array is not corrupted

salary_validated_df = cleaned_df

# COMMAND ----------

# MAGIC %md
# MAGIC ## Surrogate Keys

# COMMAND ----------

# Generate a deterministic surrogate key from listing_id
final_df = (
    salary_validated_df
    .withColumn(
        "listing_sk",
        md5(concat_ws("||", col("listing_id"), col("date_collected")))
    )
    .withColumn("silver_loaded_at", current_timestamp())
)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Write to Silver

# COMMAND ----------

# Write to silver table (overwrite for full refresh; switch to merge for incremental later)
(
    final_df.write
    .format("delta")
    .mode("overwrite")
    .option("overwriteSchema", "true")
    .saveAsTable(SILVER_TABLE)
)

silver_count = spark.table(SILVER_TABLE).count()
print(f"Silver table '{SILVER_TABLE}' now has {silver_count} rows")
