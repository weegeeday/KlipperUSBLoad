"""Tests for UsbLoadConfig parsing and validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from klipper_usb_load.config import ConfigError, UsbLoadConfig


def test_defaults_allow_all_ports() -> None:
    cfg = UsbLoadConfig.from_moonraker_config({})
    assert cfg.allowed_ports == ()
    assert cfg.is_port_allowed("1-1.1") is True
    assert cfg.max_scan_depth == 2
    assert cfg.wait_for_metadata is True
    assert cfg.sync_timeout_seconds == 60.0
    assert cfg.gcode_extensions == (".gcode", ".gco", ".g")
    assert cfg.lcd_notifications is True
    assert cfg.usb_subdir_name == "usb"


def test_parses_comma_and_newline_lists_and_clamps_bounds() -> None:
    cfg = UsbLoadConfig.from_moonraker_config(
        {
            "allowed_ports": "1-1.1, 1-1.2\n1-1.1",
            "max_scan_depth": 42,
            "wait_for_metadata": "false",
            "sync_timeout_seconds": 0.5,
            "gcode_extensions": "GCODE, .bgcode",
            "lcd_notifications": False,
            "usb_subdir_name": "usb_stick",
            "sysfs_root": "/tmp/sys",
        }
    )
    assert cfg.allowed_ports == ("1-1.1", "1-1.2")
    assert cfg.is_port_allowed("1-1.1") is True
    assert cfg.is_port_allowed("1-1.4") is False
    assert cfg.max_scan_depth == 10
    assert cfg.wait_for_metadata is False
    assert cfg.sync_timeout_seconds == 2.0
    assert cfg.gcode_extensions == (".gcode", ".bgcode")
    assert cfg.lcd_notifications is False
    assert cfg.usb_subdir_name == "usb_stick"
    assert cfg.sysfs_root == Path("/tmp/sys")


def test_rejects_unsafe_port_ids_and_subdirs() -> None:
    with pytest.raises(ConfigError):
        UsbLoadConfig.from_moonraker_config({"allowed_ports": "../etc"})

    with pytest.raises(ConfigError):
        UsbLoadConfig.from_moonraker_config({"usb_subdir_name": "../escape"})

    with pytest.raises(ConfigError):
        UsbLoadConfig.from_moonraker_config({"gcode_extensions": "bad ext!"})
