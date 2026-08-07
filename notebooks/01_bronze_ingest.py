# Databricks notebook source
# MAGIC %md
# MAGIC # Bronze Ingest
# MAGIC Read new raw JSON files from the Unity Catalog Volume → append to Delta bronze table.
# MAGIC
# MAGIC Uses Auto Loader (Structured Streaming with cloudFiles) for incremental file discovery.

# COMMAND ----------

from pyspark.sql import SparkSession
from pyspark.sql.functions import col, current_timestamp, input_file_name, lit
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

# COMMAND ----------

# Configuration
VOLUME_PATH = "/Volumes/job_market/bronze/raw_listings"
BRONZE_TABLE = "job_market.bronze.raw_job_listings"
CHECKPOINT_PATH = "/Volumes/job_market/bronze/raw_listings/_checkpoints/bronze_ingest"

# COMMAND ----------

# Schema for the raw JSON structure produced by the scraper
# Matches the output of scraper/parser.py → save_raw_output()
salary_variant_schema = StructType([
    StructField("employment_type", StringType(), True),
    StructField("salary_min", DoubleType(), True),
    StructField("salary_max", DoubleType(), True),
    StructField("currency", StringType(), True),
    StructField("is_gross", BooleanType(), True),
])

listing_schema = StructType([
    StructField("listing_id", StringType(), False),
    StructField("slug", StringType(), True),
    StructField("title", StringType(), True),
    StructField("apply_url", StringType(), True),
    StructField("company_name", StringType(), True),
    StructField("category", StringType(), True),
    StructField("seniority", StringType(), True),
    StructField("workplace_type", StringType(), True),
    StructField("cities", ArrayType(StringType()), True),
    StructField("salary_variants", ArrayType(salary_variant_schema), True),
    StructField("required_skills", ArrayType(StringType()), True),
    StructField("nice_to_have_skills", ArrayType(StringType()), True),
    StructField("description", StringType(), True),
    StructField("posted_date", StringType(), True),
    StructField("expiry_date", StringType(), True),
    StructField("date_collected", StringType(), True),
    StructField("source_run_id", StringType(), True),
])

# Wrapper schema: the scraper output wraps listings in a metadata envelope
raw_file_schema = StructType([
    StructField("metadata", StructType([
        StructField("source", StringType(), True),
        StructField("date_collected", StringType(), True),
        StructField("total_listings", IntegerType(), True),
    ]), True),
    StructField("listings", ArrayType(listing_schema), True),
])

# COMMAND ----------

# MAGIC %md
# MAGIC ## Ingest with Auto Loader
# MAGIC Auto Loader incrementally processes new files as they arrive in the Volume.
# MAGIC Using `trigger(availableNow=True)` for batch-style execution within a Workflow task.

# COMMAND ----------

# Read new files using Auto Loader (cloudFiles)
raw_df = (
    spark.readStream
    .format("cloudFiles")
    .option("cloudFiles.format", "json")
    .option("cloudFiles.schemaLocation", f"{CHECKPOINT_PATH}/schema")
    .schema(raw_file_schema)
    .load(VOLUME_PATH)
)

# COMMAND ----------

from pyspark.sql.functions import explode

# Explode the listings array and flatten
listings_df = (
    raw_df
    .select(
        col("metadata.date_collected").alias("file_date_collected"),
        col("metadata.source").alias("source"),
        input_file_name().alias("source_file"),
        explode(col("listings")).alias("listing"),
    )
    .select(
        col("listing.*"),
        col("source_file"),
        current_timestamp().alias("ingested_at"),
    )
)

# COMMAND ----------

# Write to bronze Delta table (append-only, partitioned by date_collected)
query = (
    listings_df.writeStream
    .format("delta")
    .outputMode("append")
    .option("checkpointLocation", CHECKPOINT_PATH)
    .option("mergeSchema", "true")
    .trigger(availableNow=True)
    .toTable(BRONZE_TABLE)
)

query.awaitTermination()

# COMMAND ----------

# Log results
count = spark.table(BRONZE_TABLE).count()
print(f"Bronze table '{BRONZE_TABLE}' now has {count} total rows")
