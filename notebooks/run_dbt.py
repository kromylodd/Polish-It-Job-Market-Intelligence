# Databricks notebook source
# MAGIC %md
# MAGIC # Run dbt build
# MAGIC Execute `dbt build` (staging → dims/fact/bridges → marts) as a Workflow task.

# COMMAND ----------

import subprocess
import sys

# COMMAND ----------

# dbt project is deployed as part of the bundle
DBT_PROJECT_DIR = "/Workspace/Users/{user}/dbt"  # TODO: update with actual bundle path

result = subprocess.run(
    ["dbt", "build", "--project-dir", DBT_PROJECT_DIR, "--profiles-dir", DBT_PROJECT_DIR],
    capture_output=True,
    text=True,
)

print(result.stdout)

if result.returncode != 0:
    print(result.stderr, file=sys.stderr)
    raise RuntimeError(f"dbt build failed with exit code {result.returncode}")

print("dbt build completed successfully")
