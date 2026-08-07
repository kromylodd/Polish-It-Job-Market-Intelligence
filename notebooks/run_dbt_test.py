# Databricks notebook source
# Run dbt test as a data quality gate.
#
# Same isolation strategy as run_dbt.py: dbt is invoked as a subprocess with
# PYTHONPATH pointing to /tmp/dbt_libs so typing_extensions is resolved from there.

import os
import subprocess
import sys

from pyspark.sql import SparkSession

spark = SparkSession.builder.getOrCreate()

BUNDLE_NAME = "polish-it-job-market-intelligence"
BUNDLE_TARGET = os.environ.get("DATABRICKS_BUNDLE_TARGET", "prod")
DBT_LIB_DIR = "/tmp/dbt_libs"


def resolve_dbt_project_dir() -> str:
    env_dir = os.environ.get("DBT_PROJECT_DIR")
    if env_dir:
        return env_dir
    user = spark.sql("SELECT current_user()").collect()[0][0]
    return f"/Workspace/Users/{user}/.bundle/{BUNDLE_NAME}/{BUNDLE_TARGET}/files/dbt"


def ensure_dbt_installed():
    marker = os.path.join(DBT_LIB_DIR, "dbt", "__init__.py")
    if os.path.exists(marker):
        return
    subprocess.check_call([
        sys.executable, "-m", "pip", "install",
        "--target", DBT_LIB_DIR,
        "--quiet",
        "--upgrade",
        "typing_extensions>=4.12",
        "dbt-core==1.8.4",
        "dbt-databricks==1.8.0",
    ])


ensure_dbt_installed()

DBT_PROJECT_DIR = resolve_dbt_project_dir()
print(f"Using dbt project dir: {DBT_PROJECT_DIR}")

if not os.path.isdir(DBT_PROJECT_DIR):
    raise FileNotFoundError(f"dbt project dir not found: {DBT_PROJECT_DIR}")

child_env = {
    **os.environ,
    "PYTHONPATH": f"{DBT_LIB_DIR}:{os.environ.get('PYTHONPATH', '')}",
    "DATABRICKS_HOST": os.environ.get(
        "DATABRICKS_HOST",
        spark.conf.get("spark.databricks.workspaceUrl", "dbc-b4650868-24b1.cloud.databricks.com"),
    ),
    "DATABRICKS_TOKEN": os.environ.get(
        "DATABRICKS_TOKEN",
        dbutils.notebook.entry_point.getDbutils().notebook().getContext().apiToken().get(),  # noqa: F821
    ),
}

# dbt test
print("Running dbt test...")
result = subprocess.run(
    [
        sys.executable, "-c",
        "from dbt.cli.main import dbtRunner; r = dbtRunner().invoke("
        f"['test', '--project-dir', '{DBT_PROJECT_DIR}', "
        f"'--profiles-dir', '{DBT_PROJECT_DIR}', '--target', 'databricks']); "
        "print('\\n'.join(f'{e.node.unique_id}: {e.status}' for e in (r.result or []) if hasattr(e,'node'))); "
        "raise SystemExit(0 if r.success else 1)",
    ],
    capture_output=True, text=True, env=child_env,
)

print(result.stdout)
if result.returncode != 0:
    print("=== dbt test STDERR ===")
    print(result.stderr)
    print("=== END STDERR ===")
    try:
        dbutils.notebook.exit(f"FAILED: stdout={result.stdout[-1000:]} | stderr={result.stderr[-1000:]}")  # noqa: F821
    except Exception:
        pass
    raise RuntimeError(f"dbt test failed (exit {result.returncode})")

print("dbt test completed successfully")
