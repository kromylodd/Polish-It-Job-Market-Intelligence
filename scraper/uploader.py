"""
Uploader — pushes raw JSON files to a Unity Catalog managed Volume
via the Databricks Python SDK (WorkspaceClient.files.upload).

This runs in GitHub Actions, authenticated with a Databricks PAT.
"""

import json
import logging
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path

from databricks.sdk import WorkspaceClient

logger = logging.getLogger(__name__)

# Unity Catalog Volume path
VOLUME_PATH = "/Volumes/job_market/bronze/raw_listings"


def get_workspace_client() -> WorkspaceClient:
    """
    Create a Databricks WorkspaceClient.

    Auth is resolved automatically from environment variables:
    - DATABRICKS_HOST
    - DATABRICKS_TOKEN
    """
    return WorkspaceClient()


def upload_file(
    client: WorkspaceClient,
    local_path: Path,
    volume_path: str = VOLUME_PATH,
) -> str:
    """
    Upload a local JSON file to the Unity Catalog Volume.

    Args:
        client: Authenticated WorkspaceClient.
        local_path: Path to the local file to upload.
        volume_path: Target Volume path in Unity Catalog.

    Returns:
        Full path of the uploaded file in the Volume.
    """
    remote_filename = local_path.name
    remote_path = f"{volume_path}/{remote_filename}"

    with open(local_path, "rb") as f:
        client.files.upload(remote_path, f, overwrite=True)

    logger.info(f"Uploaded {local_path} → {remote_path}")
    return remote_path


def upload_json_data(
    client: WorkspaceClient,
    data: dict,
    filename: str | None = None,
    volume_path: str = VOLUME_PATH,
) -> str:
    """
    Upload a JSON payload directly (without saving to disk first).

    Args:
        client: Authenticated WorkspaceClient.
        data: Dictionary to serialize and upload.
        filename: Optional filename. Generated with timestamp if not provided.
        volume_path: Target Volume path.

    Returns:
        Full path of the uploaded file in the Volume.
    """
    if filename is None:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        filename = f"raw_listings_{timestamp}.json"

    remote_path = f"{volume_path}/{filename}"

    json_bytes = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
    buffer = BytesIO(json_bytes)

    client.files.upload(remote_path, buffer, overwrite=True)

    logger.info(f"Uploaded {len(json_bytes)} bytes → {remote_path}")
    return remote_path


if __name__ == "__main__":
    import sys

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
