# Databricks notebook source
# Run dbt build as a Workflow task.
#
# Databricks serverless compute pre-loads a pinned typing_extensions into sys.modules
# (via PySpark). dbt requires a newer version. Since we can't override an already-
# imported module, we install dbt into /tmp/dbt_libs and invoke it as a subprocess
# with PYTHONPATH pointing there — the child process starts clean and picks up our
# version before PySpark's bootstrap taints it.

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
    """Pip-install dbt into a target dir if not already present."""
    marker = os.path.join(DBT_LIB_DIR, "dbt", "__init__.py")
    if os.path.exists(marker):
        print("dbt already present in target dir")
        return
    print("Installing dbt into isolated target dir...")
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
    print("dbt installed")


ensure_dbt_installed()

DBT_PROJECT_DIR = resolve_dbt_project_dir()
print(f"Using dbt project dir: {DBT_PROJECT_DIR}")

if not os.path.isdir(DBT_PROJECT_DIR):
    raise FileNotFoundError(f"dbt project dir not found: {DBT_PROJECT_DIR}")


# Build env for subprocess: our target dir FIRST in PYTHONPATH so typing_extensions
# is loaded from there before the system path.
# Also ensure DATABRICKS_HOST/TOKEN/HTTP_PATH are available for dbt's token-based
# auth profile. The workspace URL is resolved from the Spark conf at runtime and the
# SQL warehouse from env or a Databricks secret — nothing workspace-specific is
# hardcoded in the repo.
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

# dbt deps
print("Running dbt deps...")
deps = subprocess.run(
    [
        sys.executable,
        "-c",
        "from dbt.cli.main import dbtRunner; r = dbtRunner().invoke("
        f"['deps', '--project-dir', '{DBT_PROJECT_DIR}']); "
        "raise SystemExit(0 if r.success else 1)",
    ],
    capture_output=True,
    text=True,
    env=child_env,
)
print(deps.stdout)
if deps.returncode != 0:
    print(deps.stderr)
    print("Warning: dbt deps returned non-zero but continuing...")

# dbt build
print("Running dbt build...")
result = subprocess.run(
    [
        sys.executable,
        "-c",
        "from dbt.cli.main import dbtRunner; r = dbtRunner().invoke("
        f"['build', '--project-dir', '{DBT_PROJECT_DIR}', "
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
    print("=== dbt build STDERR ===")
    print(result.stderr)
    print("=== END STDERR ===")
    # Also exit with the error so it shows in API get-output
    try:
        dbutils.notebook.exit(f"FAILED: {result.stderr[-2000:]}")  # noqa: F821
    except Exception:
        pass
    raise RuntimeError(f"dbt build failed (exit {result.returncode})")

print("dbt build completed successfully")
