"""
Shared storage for per-user bot filter configuration.

The bot is the single writer. It keeps a fast, authoritative local JSON file
(telegram_bot/user_config.json) and mirrors it to a Databricks Unity Catalog
Volume so the daily alert senders (GitHub Actions telegram_bot/notify.py and the
Databricks notebook) can read the same preferences.

Format: {"<chat_id>": {<filter config>}, ...}

The Volume path is configurable via USER_CONFIG_VOLUME_PATH. It defaults to an
underscore-prefixed subdirectory of the raw-listings Volume. Spark Auto Loader
(used by the bronze ingest) ignores paths beginning with "_", so the config file
is never mistaken for a listings file to ingest — this avoids needing to create
a separate Volume (which Databricks Free Edition may not permit).
"""

import json
import logging
import os
import tempfile
from io import BytesIO
from pathlib import Path

logger = logging.getLogger(__name__)

LOCAL_PATH = Path(__file__).parent / "user_config.json"

DEFAULT_VOLUME_PATH = "/Volumes/job_market/bronze/raw_listings/_config/user_config.json"
VOLUME_PATH = os.environ.get("USER_CONFIG_VOLUME_PATH", DEFAULT_VOLUME_PATH)


def _looks_like_legacy(data) -> bool:
    """Pre-multi-user configs were a single flat filter dict, not {chat_id: cfg}."""
    return isinstance(data, dict) and ("tolerance" in data or "seniorities" in data)


def _coerce_store(data) -> dict:
    """Return a valid {chat_id: config} store, or {} for missing/corrupt/legacy."""
    if not isinstance(data, dict):
        return {}
    if _looks_like_legacy(data):
        logger.info("Detected legacy single-user config; migrating to per-user store")
        return {}
    return data


# --- Local file backend (authoritative for interactive use) ---


def load_local() -> dict:
    """Read the local {chat_id: config} store. Returns {} on missing/corrupt/legacy."""
    if not LOCAL_PATH.exists():
        return {}
    try:
        with open(LOCAL_PATH, encoding="utf-8") as f:
            return _coerce_store(json.load(f))
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Could not read %s (%s); starting fresh", LOCAL_PATH, e)
        return {}


def save_local(all_configs: dict):
    """Write the store atomically (temp file + os.replace)."""
    LOCAL_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(LOCAL_PATH.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(all_configs, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, LOCAL_PATH)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# --- Databricks Volume backend (best-effort mirror) ---


def volume_enabled() -> bool:
    """Whether Databricks credentials are present for Volume mirroring."""
    return bool(os.environ.get("DATABRICKS_HOST") and os.environ.get("DATABRICKS_TOKEN"))


def _workspace_client():
    from databricks.sdk import WorkspaceClient

    return WorkspaceClient()


def upload_to_volume(all_configs: dict) -> bool:
    """Publish the store to the Volume. Returns True on success, False otherwise.

    Best-effort: never raises. The local copy remains authoritative, so a failed
    mirror just means the daily sender uses slightly older preferences.
    """
    if not volume_enabled():
        return False
    try:
        client = _workspace_client()
        parent = VOLUME_PATH.rsplit("/", 1)[0]
        try:
            client.files.create_directory(parent)
        except Exception:
            pass  # Already exists or not required.
        payload = json.dumps(all_configs, ensure_ascii=False, indent=2).encode("utf-8")
        client.files.upload(VOLUME_PATH, BytesIO(payload), overwrite=True)
        logger.info("Published user config to %s (%d users)", VOLUME_PATH, len(all_configs))
        return True
    except Exception as e:
        logger.warning("Failed to publish user config to Volume: %s", e)
        return False


def download_from_volume() -> dict | None:
    """Fetch the store from the Volume. Returns None if unavailable/empty."""
    if not volume_enabled():
        return None
    try:
        client = _workspace_client()
        resp = client.files.download(VOLUME_PATH)
        raw = resp.contents.read()
        store = _coerce_store(json.loads(raw))
        return store or None
    except Exception as e:
        logger.info("Could not download user config from Volume: %s", e)
        return None
