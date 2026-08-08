"""
Upload raw JSON files to Unity Catalog managed Volume via Databricks SDK.

Runs in GitHub Actions, authenticated via DATABRICKS_HOST + DATABRICKS_TOKEN env vars.

The daily scrape produces a large (~30-40 MB) JSON file. Uploading that directly
from a GitHub runner to a Free-Edition Databricks workspace is slow and flaky:
the workspace is often cold (SSL/EOF churn while it wakes) and a large payload
over a datacenter TLS path frequently stalls mid-transfer, forcing long retries.

Two mitigations:
  * gzip the payload before upload (JSON compresses ~10x, so each attempt is a
    few-MB transfer that completes in seconds). Spark / Auto Loader reads
    ".json.gz" transparently by extension, so the bronze ingest is unchanged.
  * "prewarm" the workspace with a cheap, retried call first, so the cold-start
    churn happens on a tiny request instead of the big upload.

Retries remain for any residual transient SSL/connection errors.
"""

import gzip
import logging
import os
import sys
import time
from io import BytesIO
from pathlib import Path

from databricks.sdk import WorkspaceClient

logger = logging.getLogger(__name__)

VOLUME_PATH = "/Volumes/job_market/bronze/raw_listings"

MAX_UPLOAD_RETRIES = 5
RETRY_BACKOFF_SECONDS = 30  # wait between retries (workspace may be waking up)

# Substrings that identify transient errors worth retrying when the SDK wraps the
# underlying exception in a generic type.
_TRANSIENT_MARKERS = ("ssl", "timed out", "eof", "connection reset", "broken pipe")

# Deterministic Databricks SDK errors — retrying these is pointless and just wastes
# minutes. A 404/401/403 here almost always means DATABRICKS_HOST or DATABRICKS_TOKEN
# is wrong (e.g. a stale GitHub secret pointing at a deleted/other workspace).
_FATAL_ERROR_NAMES = frozenset(
    {
        "NotFound",
        "Unauthenticated",
        "PermissionDenied",
        "BadRequest",
        "ResourceDoesNotExist",
        "InvalidParameterValue",
    }
)


def get_workspace_client() -> WorkspaceClient:
    """Create a WorkspaceClient from env vars, sanitizing the host.

    Trailing whitespace/slashes in a pasted secret can silently break auth, so we
    normalize DATABRICKS_HOST and pass host/token explicitly. The resolved host is
    logged (it's a URL, not a credential) so a wrong secret is obvious in the logs.
    """
    host = os.environ.get("DATABRICKS_HOST", "").strip().rstrip("/")
    token = os.environ.get("DATABRICKS_TOKEN", "").strip()
    if host and not host.startswith("http"):
        host = f"https://{host}"

    if host and token:
        logger.info("Databricks host: %s", host)
        return WorkspaceClient(host=host, token=token)

    logger.warning("DATABRICKS_HOST/DATABRICKS_TOKEN not both set; using SDK default auth")
    return WorkspaceClient()


def _is_transient(exc: Exception) -> bool:
    """Whether an exception is worth retrying (transient network/SSL)."""
    if type(exc).__name__ in _FATAL_ERROR_NAMES:
        return False
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return True
    err = str(exc).lower()
    return any(marker in err for marker in _TRANSIENT_MARKERS)


def _with_retries(action, description: str):
    """Run ``action`` with retry/backoff on transient errors. Returns its result."""
    for attempt in range(1, MAX_UPLOAD_RETRIES + 1):
        try:
            return action()
        except Exception as e:
            if not _is_transient(e) or attempt == MAX_UPLOAD_RETRIES:
                if type(e).__name__ in _FATAL_ERROR_NAMES:
                    logger.error(
                        "%s failed with a non-transient error (%s: %s). "
                        "Check the DATABRICKS_HOST and DATABRICKS_TOKEN secrets — a 404/401 "
                        "usually means they point at the wrong or a deleted workspace.",
                        description,
                        type(e).__name__,
                        e,
                    )
                raise
            wait = RETRY_BACKOFF_SECONDS * attempt
            logger.warning(
                "%s attempt %d/%d failed (%s); retrying in %ds (workspace may be waking)...",
                description,
                attempt,
                MAX_UPLOAD_RETRIES,
                e,
                wait,
            )
            time.sleep(wait)
    # Unreachable: the loop either returns or raises.
    raise RuntimeError(f"{description} failed after {MAX_UPLOAD_RETRIES} attempts")


def prewarm(client: WorkspaceClient) -> None:
    """Wake a possibly-idle workspace with a cheap, retried call before uploading."""

    def _ping():
        client.current_user.me()
        return True

    try:
        _with_retries(_ping, "Workspace prewarm")
        logger.info("Workspace is awake")
    except Exception as e:  # Non-fatal: the upload's own retries can still recover.
        logger.warning("Prewarm did not confirm the workspace is awake: %s", e)


def _gzip_file(local_path: Path) -> bytes:
    """Read and gzip-compress a file into memory."""
    with open(local_path, "rb") as f:
        return gzip.compress(f.read(), compresslevel=6)


def upload_file(
    client: WorkspaceClient,
    local_path: Path,
    volume_path: str = VOLUME_PATH,
) -> str:
    """Gzip-compress and upload a file to the Volume with retries. Returns remote path."""
    remote_path = f"{volume_path}/{local_path.name}.gz"
    payload = _gzip_file(local_path)
    logger.info(
        "Compressed %s: %.1f MB -> %.1f MB",
        local_path.name,
        local_path.stat().st_size / 1e6,
        len(payload) / 1e6,
    )

    def _do_upload():
        client.files.upload(remote_path, BytesIO(payload), overwrite=True)
        logger.info(f"Uploaded {local_path.name}.gz -> {remote_path}")
        return remote_path

    return _with_retries(_do_upload, f"Upload {local_path.name}.gz")


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
    prewarm(client)
    remote = upload_file(client, filepath)
    print(f"Uploaded to: {remote}")
