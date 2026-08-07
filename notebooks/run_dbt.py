# Databricks notebook source
# Run dbt build as a Workflow task.

import subprocess
import sys

DBT_PROJECT_DIR = "/Workspace/Users/{user}/dbt"  # update with actual bundle path

result = subprocess.run(
    ["dbt", "build", "--project-dir", DBT_PROJECT_DIR, "--profiles-dir", DBT_PROJECT_DIR],
    capture_output=True,
    text=True,
)
print(result.stdout)
if result.returncode != 0:
    print(result.stderr, file=sys.stderr)
    raise RuntimeError(f"dbt build failed (exit {result.returncode})")
