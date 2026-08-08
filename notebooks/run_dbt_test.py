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
    subprocess.check_call(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--target",
            DBT_LIB_DIR,
            "--quiet",
            "--upgrade",
            "typing_extensions>=4.12",
            "dbt-core==1.8.4",
            "dbt-databricks==1.8.0",
        ]
    )


ensure_dbt_installed()

DBT_PROJECT_DIR = resolve_dbt_project_dir()
print(f"Using dbt project dir: {DBT_PROJECT_DIR}")

if not os.path.isdir(DBT_PROJECT_DIR):
    raise FileNotFoundError(f"dbt project dir not found: {DBT_PROJECT_DIR}")


def _resolve_http_path() -> str:
    """SQL warehouse http_path for dbt.

    Resolution order: DATABRICKS_HTTP_PATH env -> DATABRICKS_WAREHOUSE_ID env ->
    `warehouse_id` in the `job_market` Databricks secret scope.
    """
    path = os.environ.get("DATABRICKS_HTTP_PATH")
    if path:
        return path
    warehouse_id = os.environ.get("DATABRICKS_WAREHOUSE_ID", "")
    if not warehouse_id:
        try:
            warehouse_id = dbutils.secrets.get("job_market", "warehouse_id")  # noqa: F821
        except Exception:
            warehouse_id = ""
    return f"/sql/1.0/warehouses/{warehouse_id}" if warehouse_id else ""


_http_path = _resolve_http_path()


def _resolve_token() -> str:
    """Return DATABRICKS_TOKEN from env, falling back to the notebook API token."""
    token = os.environ.get("DATABRICKS_TOKEN")
    if token:
        return token
    ctx = dbutils.notebook.entry_point.getDbutils().notebook().getContext()  # noqa: F821
    return ctx.apiToken().get()


child_env = {
    **os.environ,
    "PYTHONPATH": f"{DBT_LIB_DIR}:{os.environ.get('PYTHONPATH', '')}",
    "DATABRICKS_HOST": os.environ.get(
        "DATABRICKS_HOST",
        spark.conf.get("spark.databricks.workspaceUrl"),
    ),
    "DATABRICKS_HTTP_PATH": _http_path,
    "DATABRICKS_TOKEN": _resolve_token(),
}

# dbt test
print("Running dbt test...")
result = subprocess.run(
    [
        sys.executable,
        "-c",
        "from dbt.cli.main import dbtRunner; r = dbtRunner().invoke("
        f"['test', '--project-dir', '{DBT_PROJECT_DIR}', "
        f"'--profiles-dir', '{DBT_PROJECT_DIR}', '--target', 'databricks']); "
        "print('\\n'.join(f'{e.node.unique_id}: {e.status}' for e in (r.result or []) if hasattr(e,'node'))); "
        "raise SystemExit(0 if r.success else 1)",
    ],
    capture_output=True,
    text=True,
    env=child_env,
)

print(result.stdout)
if result.returncode != 0:
    print("=== dbt test STDERR ===")
    print(result.stderr)
    print("=== END STDERR ===")
    try:
        _msg = f"FAILED: stdout={result.stdout[-1000:]} | stderr={result.stderr[-1000:]}"
        dbutils.notebook.exit(_msg)  # noqa: F821
    except Exception:
        pass
    raise RuntimeError(f"dbt test failed (exit {result.returncode})")

print("dbt test completed successfully")
