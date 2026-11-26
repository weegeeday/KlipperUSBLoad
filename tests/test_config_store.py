"""Tests for SettingsStore atomic JSON persistence and validation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from klipper_usb_load.config_store import SettingsStore, SettingsValidationError


def test_load_creates_default_file_and_clamps_invalid_disk_values(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config" / "klipper_usb_load.json"
    store = SettingsStore(cfg_path)
    loaded = store.load()

    assert cfg_path.exists()
    assert loaded.port_policy == "allow_all"
    assert loaded.wait_for_metadata is True
    assert loaded.max_scan_depth == 2
    assert loaded.sync_timeout_seconds == 60.0
    assert loaded.gcode_extensions == [".gcode", ".gco", ".g"]

    # Corrupt some fields on disk and verify graceful clamping/fallback on reload
    cfg_path.write_text(
        json.dumps(
            {
                "port_policy": "allowlist",
                "allowed_ports": ["1-1.2", "1-1.2", "1-1.4"],
                "max_scan_depth": 99,
                "sync_timeout_seconds": 0.1,
                "gcode_extensions": ["GCODE", ".bgcode"],
            }
        ),
        encoding="utf-8",
    )
    reloaded = store.load()
    assert reloaded.port_policy == "allowlist"
    assert reloaded.allowed_ports == ["1-1.2", "1-1.4"]
    assert reloaded.max_scan_depth == 10
    assert reloaded.sync_timeout_seconds == 2.0
    assert reloaded.gcode_extensions == [".gcode", ".bgcode"]


def test_port_allowlist_and_strict_update_validation(tmp_path: Path) -> None:
    cfg_path = tmp_path / "klipper_usb_load.json"
    store = SettingsStore(cfg_path)
    store.load()

    assert store.is_port_allowed("1-1.1") is True

    updated = store.update(
        {
            "port_policy": "allowlist",
            "allowed_ports": ["1-1.3"],
            "port_labels": {"1-1.3": "  Front USB  ", "1-1.4": ""},
            "wait_for_metadata": False,
        }
    )
    assert updated.port_policy == "allowlist"
    assert updated.port_labels == {"1-1.3": "Front USB"}
    assert store.is_port_allowed("1-1.3") is True
    assert store.is_port_allowed("1-1.2") is False

    # Reload from disk to confirm atomic write succeeded
    reloaded = SettingsStore(cfg_path).load()
    assert reloaded.allowed_ports == ["1-1.3"]
    assert reloaded.wait_for_metadata is False

    with pytest.raises(SettingsValidationError):
        store.update({"port_policy": "invalid_policy"})

    with pytest.raises(SettingsValidationError):
        store.update({"allowed_ports": ["../etc/passwd"]})

    with pytest.raises(SettingsValidationError):
        store.update({"usb_subdir_name": "../escape"})

    with pytest.raises(SettingsValidationError):
        store.update({"gcode_extensions": []})
