# Databricks notebook source
# Run dbt build as a Workflow task.

import os
import subprocess
import sys

from pyspark.sql import SparkSession

spark = SparkSession.builder.getOrCreate()

BUNDLE_NAME = "polish-it-job-market-intelligence"
BUNDLE_TARGET = os.environ.get("DATABRICKS_BUNDLE_TARGET", "prod")


def resolve_dbt_project_dir() -> str:
    """Resolve the deployed dbt project directory.

    An explicit DBT_PROJECT_DIR env var wins. Otherwise derive the Asset
    Bundle path from the current workspace user + target, i.e.
    /Workspace/Users/<user>/.bundle/<bundle>/<target>/files/dbt
    """
    env_dir = os.environ.get("DBT_PROJECT_DIR")
    if env_dir:
        return env_dir
    user = spark.sql("SELECT current_user()").collect()[0][0]
    return f"/Workspace/Users/{user}/.bundle/{BUNDLE_NAME}/{BUNDLE_TARGET}/files/dbt"


DBT_PROJECT_DIR = resolve_dbt_project_dir()
print(f"Using dbt project dir: {DBT_PROJECT_DIR}")

result = subprocess.run(
    ["dbt", "build", "--project-dir", DBT_PROJECT_DIR, "--profiles-dir", DBT_PROJECT_DIR],
    capture_output=True,
    text=True,
)
print(result.stdout)
if result.returncode != 0:
    print(result.stderr, file=sys.stderr)
    raise RuntimeError(f"dbt build failed (exit {result.returncode})")
