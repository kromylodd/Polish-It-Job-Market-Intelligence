# Databricks notebook source
# Run dbt test as a data quality gate. Fails the job if tests fail.

import subprocess
import sys

DBT_PROJECT_DIR = "/Workspace/Users/{user}/dbt"  # update with actual bundle path

result = subprocess.run(
    ["dbt", "test", "--project-dir", DBT_PROJECT_DIR, "--profiles-dir", DBT_PROJECT_DIR],
    capture_output=True,
    text=True,
)
print(result.stdout)
if result.returncode != 0:
    print(result.stderr, file=sys.stderr)
    raise RuntimeError(f"dbt test failed (exit {result.returncode})")
