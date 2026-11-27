"""INI/Moonraker configuration parsing and validation for KlipperUSBLoad."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_PORT_ID_RE = re.compile(r"^[A-Za-z0-9._:-]+$")
_SUBDIR_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_EXT_RE = re.compile(r"^\.[a-z0-9]{1,16}$")

DEFAULT_EXTENSIONS: tuple[str, ...] = (".gcode", ".gco", ".g")


class ConfigError(ValueError):
    """Raised when KlipperUSBLoad configuration values are invalid."""


def _split_tokens(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, (list, tuple, set)):
        tokens: list[str] = []
        for item in raw:
            tokens.extend(_split_tokens(item))
        return tokens
    text = str(raw).strip()
    if not text:
        return []
    return [part for part in re.split(r"[\s,;]+", text) if part]


def _parse_allowed_ports(raw: Any) -> tuple[str, ...]:
    ports: list[str] = []
    seen: set[str] = set()
    for token in _split_tokens(raw):
        if ".." in token or not _PORT_ID_RE.match(token):
            raise ConfigError(f"Invalid USB port ID in allowed_ports: {token!r}")
        if token not in seen:
            seen.add(token)
            ports.append(token)
    return tuple(ports)


def _parse_extensions(raw: Any) -> tuple[str, ...]:
    tokens = _split_tokens(raw)
    if not tokens:
        return DEFAULT_EXTENSIONS
    result: list[str] = []
    seen: set[str] = set()
    for token in tokens:
        ext = token.lower()
        if not ext.startswith("."):
            ext = f".{ext}"
        if not _EXT_RE.match(ext):
            raise ConfigError(f"Invalid G-code extension: {token!r}")
        if ext not in seen:
            seen.add(ext)
            result.append(ext)
    return tuple(result) if result else DEFAULT_EXTENSIONS


def _parse_subdir(raw: Any) -> str:
    cleaned = str(raw if raw is not None else "usb").strip()
    if not cleaned or cleaned in (".", "..") or not _SUBDIR_RE.match(cleaned):
        raise ConfigError(f"Invalid usb_subdir_name: {raw!r}")
    return cleaned


def _read_raw(config: Any, key: str, default: Any = None) -> Any:
    if config is None:
        return default
    if isinstance(config, dict):
        return config.get(key, default)
    getter = getattr(config, "get", None)
    if callable(getter):
        try:
            return getter(key, default)
        except TypeError:
            return default
    return default


def _read_int(config: Any, key: str, default: int) -> int:
    if isinstance(config, dict):
        val = config.get(key, default)
        return default if val is None else int(val)
    getter = getattr(config, "getint", None)
    if callable(getter):
        return int(getter(key, default))
    raw = _read_raw(config, key, default)
    return default if raw is None else int(raw)


def _read_float(config: Any, key: str, default: float) -> float:
    if isinstance(config, dict):
        val = config.get(key, default)
        return default if val is None else float(val)
    getter = getattr(config, "getfloat", None)
    if callable(getter):
        return float(getter(key, default))
    raw = _read_raw(config, key, default)
    return default if raw is None else float(raw)


def _read_bool(config: Any, key: str, default: bool) -> bool:
    if isinstance(config, dict):
        val = config.get(key, default)
        if isinstance(val, bool):
            return val
        if isinstance(val, str):
            return val.strip().lower() in ("1", "true", "yes", "on")
        return bool(val)
    getter = getattr(config, "getboolean", None)
    if callable(getter):
        return bool(getter(key, default))
    raw = _read_raw(config, key, default)
    return bool(raw)


@dataclass(frozen=True)
class UsbLoadConfig:
    """Configuration for KlipperUSBLoad loaded from [klipper_usb_load]."""

    allowed_ports: tuple[str, ...] = ()
    max_scan_depth: int = 2
    wait_for_metadata: bool = True
    sync_timeout_seconds: float = 60.0
    gcode_extensions: tuple[str, ...] = DEFAULT_EXTENSIONS
    lcd_notifications: bool = True
    usb_subdir_name: str = "usb"
    sysfs_root: Path = Path("/sys")
    mount_base: Path = Path("/run/klipper-usb-load")
    gcodes_path: Path | None = None
    udev_token_path: Path | None = None

    def is_port_allowed(self, port_id: str) -> bool:
        """Return True when allowed_ports is empty (allow all) or contains port_id."""
        if not self.allowed_ports:
            return True
        return port_id in self.allowed_ports

    @classmethod
    def from_moonraker_config(cls, config: Any) -> UsbLoadConfig:
        """Build a validated UsbLoadConfig from Moonraker ConfigHelper or dict."""
        raw_gcodes = _read_raw(config, "gcodes_path", None)
        raw_token = _read_raw(config, "udev_token_path", None)
        depth = max(0, min(10, _read_int(config, "max_scan_depth", 2)))
        timeout = max(2.0, min(600.0, _read_float(config, "sync_timeout_seconds", 60.0)))

        return cls(
            allowed_ports=_parse_allowed_ports(_read_raw(config, "allowed_ports", "")),
            max_scan_depth=depth,
            wait_for_metadata=_read_bool(config, "wait_for_metadata", True),
            sync_timeout_seconds=timeout,
            gcode_extensions=_parse_extensions(_read_raw(config, "gcode_extensions", "")),
            lcd_notifications=_read_bool(config, "lcd_notifications", True),
            usb_subdir_name=_parse_subdir(_read_raw(config, "usb_subdir_name", "usb")),
            sysfs_root=Path(str(_read_raw(config, "sysfs_root", "/sys"))),
            mount_base=Path(str(_read_raw(config, "mount_base", "/run/klipper-usb-load"))),
            gcodes_path=Path(str(raw_gcodes)) if raw_gcodes else None,
            udev_token_path=Path(str(raw_token)) if raw_token else None,
        )
