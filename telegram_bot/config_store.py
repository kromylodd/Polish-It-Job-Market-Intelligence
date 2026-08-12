"""
Shared storage for per-user bot filter configuration.

The bot is the single writer. It keeps a fast, authoritative local JSON file
(telegram_bot/user_config.json). The daily broadcast (telegram_bot/notify.py)
runs in-process on the same host and reads this same file directly, so there is
no external mirror to keep in sync.

Format: {"<chat_id>": {<filter config>}, ...}
"""

import json
import logging
import os
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

LOCAL_PATH = Path(__file__).parent / "user_config.json"


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
