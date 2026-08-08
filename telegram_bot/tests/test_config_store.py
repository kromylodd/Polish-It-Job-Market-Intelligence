"""Tests for the shared config store (local backend + Volume guards)."""

import pytest

from telegram_bot import config_store


@pytest.fixture
def local_store(tmp_path, monkeypatch):
    monkeypatch.setattr(config_store, "LOCAL_PATH", tmp_path / "user_config.json")
    return config_store


def test_load_missing_returns_empty(local_store):
    assert local_store.load_local() == {}


def test_save_then_load_roundtrip(local_store):
    store = {"111": {"tolerance": 0, "salary_min": 5000}, "222": {"tolerance": 2}}
    local_store.save_local(store)
    assert local_store.load_local() == store


def test_legacy_flat_config_is_migrated_to_empty(local_store):
    # A pre-multi-user flat config must not be treated as a per-user store.
    local_store.save_local({"tolerance": 2, "salary_min": 123, "seniorities": ["junior"]})
    assert local_store.load_local() == {}


def test_corrupt_file_returns_empty(local_store):
    local_store.LOCAL_PATH.write_text("{ not valid json")
    assert local_store.load_local() == {}


def test_save_is_atomic_no_tmp_left_behind(local_store):
    local_store.save_local({"1": {"tolerance": 1}})
    leftovers = list(local_store.LOCAL_PATH.parent.glob("*.tmp"))
    assert leftovers == []


def test_volume_disabled_without_credentials(monkeypatch):
    monkeypatch.delenv("DATABRICKS_HOST", raising=False)
    monkeypatch.delenv("DATABRICKS_TOKEN", raising=False)
    assert config_store.volume_enabled() is False
    # Guards short-circuit before any network/SDK use.
    assert config_store.upload_to_volume({"1": {}}) is False
    assert config_store.download_from_volume() is None
