# Databricks notebook source
# MAGIC %md
# MAGIC # Run dbt test
# MAGIC Execute `dbt test` as a data quality gate. Job fails if tests fail.

# COMMAND ----------

import subprocess
import sys

# COMMAND ----------

DBT_PROJECT_DIR = "/Workspace/Users/{user}/dbt"  # TODO: update with actual bundle path

result = subprocess.run(
    ["dbt", "test", "--project-dir", DBT_PROJECT_DIR, "--profiles-dir", DBT_PROJECT_DIR],
    capture_output=True,
    text=True,
)

print(result.stdout)

if result.returncode != 0:
    print(result.stderr, file=sys.stderr)
    print("\n⚠️ DATA QUALITY GATE FAILED — check test results above")
    raise RuntimeError(f"dbt test failed with exit code {result.returncode}")

print("✅ All dbt tests passed — data quality gate OK")
