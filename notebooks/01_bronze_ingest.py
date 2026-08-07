# Databricks notebook source
# Bronze ingest: read new JSON files from Volume -> append to Delta table.
# Uses Auto Loader (cloudFiles) for incremental file discovery.

from pyspark.sql import SparkSession
from pyspark.sql.functions import col, current_timestamp, explode
from pyspark.sql.types import (
    ArrayType,
    BooleanType,
    DoubleType,
    IntegerType,
    StringType,
    StructField,
    StructType,
)

spark = SparkSession.builder.getOrCreate()

VOLUME_PATH = "/Volumes/job_market/bronze/raw_listings"
BRONZE_TABLE = "job_market.bronze.raw_job_listings"
CHECKPOINT_PATH = "/Volumes/job_market/bronze/raw_listings/_checkpoints/bronze_ingest"

# Schema matching scraper output
salary_variant_schema = StructType(
    [
        StructField("employment_type", StringType(), True),
        StructField("salary_min", DoubleType(), True),
        StructField("salary_max", DoubleType(), True),
        StructField("currency", StringType(), True),
        StructField("unit", StringType(), True),
        StructField("is_gross", BooleanType(), True),
    ]
)

listing_schema = StructType(
    [
        StructField("listing_id", StringType(), False),
        StructField("slug", StringType(), True),
        StructField("title", StringType(), True),
        StructField("apply_url", StringType(), True),
        StructField("apply_method", StringType(), True),
        StructField("company_name", StringType(), True),
        StructField("category", StringType(), True),
        StructField("seniority", StringType(), True),
        StructField("workplace_type", StringType(), True),
        StructField("working_time", StringType(), True),
        StructField("cities", ArrayType(StringType()), True),
        StructField("salary_variants", ArrayType(salary_variant_schema), True),
        StructField("required_skills", ArrayType(StringType()), True),
        StructField("nice_to_have_skills", ArrayType(StringType()), True),
        StructField("description", StringType(), True),
        StructField("posted_date", StringType(), True),
        StructField("last_published_date", StringType(), True),
        StructField("expiry_date", StringType(), True),
        StructField("is_promoted", BooleanType(), True),
        StructField("is_super_offer", BooleanType(), True),
        StructField("is_remote_interview", BooleanType(), True),
        StructField("date_collected", StringType(), True),
        StructField("source_run_id", StringType(), True),
    ]
)

raw_file_schema = StructType(
    [
        StructField(
            "metadata",
            StructType(
                [
                    StructField("source", StringType(), True),
                    StructField("date_collected", StringType(), True),
                    StructField("total_listings", IntegerType(), True),
                ]
            ),
            True,
        ),
        StructField("listings", ArrayType(listing_schema), True),
    ]
)

# Ingest with Auto Loader
raw_df = (
    spark.readStream.format("cloudFiles")
    .option("cloudFiles.format", "json")
    .option("cloudFiles.schemaLocation", f"{CHECKPOINT_PATH}/schema")
    .schema(raw_file_schema)
    .load(VOLUME_PATH)
)

listings_df = raw_df.select(
    col("metadata.date_collected").alias("file_date_collected"),
    col("metadata.source").alias("source"),
    col("_metadata.file_path").alias("source_file"),
    explode(col("listings")).alias("listing"),
).select(
    col("listing.*"),
    col("source_file"),
    current_timestamp().alias("ingested_at"),
)

query = (
    listings_df.writeStream.format("delta")
    .outputMode("append")
    .option("checkpointLocation", CHECKPOINT_PATH)
    .option("mergeSchema", "true")
    .trigger(availableNow=True)
    .toTable(BRONZE_TABLE)
)
query.awaitTermination()

count = spark.table(BRONZE_TABLE).count()
print(f"Bronze table: {count} rows")
