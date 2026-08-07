# Databricks notebook source
# Silver clean: deduplicate, standardize, remove invalid records.

from pyspark.sql import SparkSession, Window
from pyspark.sql.functions import col, current_timestamp, lower, row_number, trim, when

spark = SparkSession.builder.getOrCreate()

BRONZE_TABLE = "job_market.bronze.raw_job_listings"
SILVER_TABLE = "job_market.silver.stg_listings"

bronze_df = spark.table(BRONZE_TABLE)
print(f"Bronze: {bronze_df.count()} rows")

# Deduplicate: keep latest per listing_id
window = Window.partitionBy("listing_id").orderBy(col("date_collected").desc())
deduped_df = (
    bronze_df.withColumn("_rn", row_number().over(window)).filter(col("_rn") == 1).drop("_rn")
)
print(f"After dedup: {deduped_df.count()} rows")

# Clean
cleaned_df = (
    deduped_df.withColumn("category", trim(col("category")))
    .withColumn("seniority", lower(trim(col("seniority"))))
    .withColumn("workplace_type", lower(trim(col("workplace_type"))))
    # Standardize workplace_type to a single canonical vocabulary.
    # justjoin.it uses "office" for on-site roles; collapse it to "onsite".
    .withColumn(
        "workplace_type",
        when(col("workplace_type") == "office", "onsite").otherwise(col("workplace_type")),
    )
    .filter(col("listing_id").isNotNull())
    .filter(col("title").isNotNull() & (col("title") != ""))
    .filter(col("company_name").isNotNull() & (col("company_name") != ""))
)

# Note: the surrogate key is generated downstream in dbt (stg_listings via
# dbt_utils.generate_surrogate_key), so we intentionally don't compute one here
# to avoid two competing definitions of the same key.
final_df = cleaned_df.withColumn("silver_loaded_at", current_timestamp())

# Write
(
    final_df.write.format("delta")
    .mode("overwrite")
    .option("overwriteSchema", "true")
    .saveAsTable(SILVER_TABLE)
)

print(f"Silver table: {spark.table(SILVER_TABLE).count()} rows")
