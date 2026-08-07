# Databricks notebook source
# Silver clean: deduplicate, standardize, remove invalid records.

from pyspark.sql import SparkSession, Window
from pyspark.sql.functions import col, concat_ws, current_timestamp, lower, md5, row_number, trim

spark = SparkSession.builder.getOrCreate()

BRONZE_TABLE = "job_market.bronze.raw_job_listings"
SILVER_TABLE = "job_market.silver.stg_listings"

bronze_df = spark.table(BRONZE_TABLE)
print(f"Bronze: {bronze_df.count()} rows")

# Deduplicate: keep latest per listing_id
window = Window.partitionBy("listing_id").orderBy(col("date_collected").desc())
deduped_df = (
    bronze_df
    .withColumn("_rn", row_number().over(window))
    .filter(col("_rn") == 1)
    .drop("_rn")
)
print(f"After dedup: {deduped_df.count()} rows")

# Clean
cleaned_df = (
    deduped_df
    .withColumn("category", trim(col("category")))
    .withColumn("seniority", lower(trim(col("seniority"))))
    .withColumn("workplace_type", lower(trim(col("workplace_type"))))
    .filter(col("listing_id").isNotNull())
    .filter(col("title").isNotNull() & (col("title") != ""))
    .filter(col("company_name").isNotNull() & (col("company_name") != ""))
)

# Surrogate key
final_df = (
    cleaned_df
    .withColumn("listing_sk", md5(concat_ws("||", col("listing_id"), col("date_collected"))))
    .withColumn("silver_loaded_at", current_timestamp())
)

# Write
(
    final_df.write
    .format("delta")
    .mode("overwrite")
    .option("overwriteSchema", "true")
    .saveAsTable(SILVER_TABLE)
)

print(f"Silver table: {spark.table(SILVER_TABLE).count()} rows")
