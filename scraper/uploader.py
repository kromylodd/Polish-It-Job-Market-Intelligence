"""
Upload raw JSON files to Unity Catalog managed Volume via Databricks SDK.

Runs in GitHub Actions, authenticated via DATABRICKS_HOST + DATABRICKS_TOKEN env vars.
"""

import json
import logging
import sys
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path

from databricks.sdk import WorkspaceClient

logger = logging.getLogger(__name__)

VOLUME_PATH = "/Volumes/job_market/bronze/raw_listings"


def get_workspace_client() -> WorkspaceClient:
    """Create WorkspaceClient. Auth resolved from env vars."""
    return WorkspaceClient()


def upload_file(
    client: WorkspaceClient,
    local_path: Path,
    volume_path: str = VOLUME_PATH,
) -> str:
    """Upload local file to Volume. Returns remote path."""
    remote_path = f"{volume_path}/{local_path.name}"
    with open(local_path, "rb") as f:
        client.files.upload(remote_path, f, overwrite=True)
    logger.info(f"Uploaded {local_path} -> {remote_path}")
    return remote_path


def upload_json_data(
    client: WorkspaceClient,
    data: dict,
    filename: str | None = None,
    volume_path: str = VOLUME_PATH,
) -> str:
    """Upload dict as JSON directly to Volume."""
    if filename is None:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        filename = f"raw_listings_{timestamp}.json"

    remote_path = f"{volume_path}/{filename}"
    json_bytes = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
    client.files.upload(remote_path, BytesIO(json_bytes), overwrite=True)
    logger.info(f"Uploaded {len(json_bytes)} bytes -> {remote_path}")
    return remote_path


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if len(sys.argv) < 2:
        print("Usage: python uploader.py <path_to_json_file>")
        sys.exit(1)

    filepath = Path(sys.argv[1])
    if not filepath.exists():
        print(f"File not found: {filepath}")
        sys.exit(1)

    client = get_workspace_client()
    remote = upload_file(client, filepath)
    print(f"Uploaded to: {remote}")
