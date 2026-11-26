"""Atomic JSON persistence and validation for KlipperUSBLoad settings."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Optional

from .models import PluginSettings, PortPolicy

_PORT_ID_RE = re.compile(r"^[A-Za-z0-9._:-]+$")
_SUBDIR_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_EXT_RE = re.compile(r"^\.[a-z0-9]{1,16}$")

DEFAULT_EXTENSIONS = [".gcode", ".gco", ".g"]


class SettingsValidationError(ValueError):
    """Raised when incoming settings fail validation."""


def _validate_port_id(value: str) -> str:
    cleaned = value.strip()
    if not cleaned or ".." in cleaned or not _PORT_ID_RE.match(cleaned):
        raise SettingsValidationError(f"Invalid USB port ID: {value!r}")
    return cleaned


def _validate_policy(value: Any) -> PortPolicy:
    if value not in ("allow_all", "allowlist"):
        raise SettingsValidationError(
            f"port_policy must be 'allow_all' or 'allowlist', got {value!r}"
        )
    if value == "allow_all":
        return "allow_all"
    return "allowlist"


def _validate_extensions(value: Any) -> list[str]:
    if not isinstance(value, list):
        raise SettingsValidationError("gcode_extensions must be a list of strings")
    result: list[str] = []
    seen: set[str] = set()
    for raw in value:
        if not isinstance(raw, str):
            raise SettingsValidationError("Each gcode extension must be a string")
        ext = raw.strip().lower()
        if not ext:
            continue
        if not ext.startswith("."):
            ext = f".{ext}"
        if not _EXT_RE.match(ext):
            raise SettingsValidationError(f"Invalid G-code extension: {raw!r}")
        if ext not in seen:
            seen.add(ext)
            result.append(ext)
    if not result:
        raise SettingsValidationError("At least one valid G-code extension is required")
    return result


def _validate_subdir_name(value: Any) -> str:
    if not isinstance(value, str):
        raise SettingsValidationError("usb_subdir_name must be a string")
    cleaned = value.strip()
    if not cleaned or cleaned in (".", "..") or not _SUBDIR_RE.match(cleaned):
        raise SettingsValidationError(
            f"usb_subdir_name must match ^[A-Za-z0-9_-]+$, got {value!r}"
        )
    return cleaned


def _clamp_scan_depth(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SettingsValidationError("max_scan_depth must be an integer")
    depth = int(value)
    return max(0, min(10, depth))


def _clamp_timeout(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SettingsValidationError("sync_timeout_seconds must be a number")
    timeout = float(value)
    return max(2.0, min(600.0, timeout))


def _validate_allowed_ports(value: Any) -> list[str]:
    if not isinstance(value, list):
        raise SettingsValidationError("allowed_ports must be a list of port IDs")
    result: list[str] = []
    seen: set[str] = set()
    for raw in value:
        if not isinstance(raw, str):
            raise SettingsValidationError("Each port ID in allowed_ports must be a string")
        port_id = _validate_port_id(raw)
        if port_id not in seen:
            seen.add(port_id)
            result.append(port_id)
    return result


def _validate_port_labels(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        raise SettingsValidationError("port_labels must be an object mapping port_id -> label")
    result: dict[str, str] = {}
    for raw_key, raw_label in value.items():
        if not isinstance(raw_key, str) or not isinstance(raw_label, str):
            raise SettingsValidationError("port_labels keys and values must be strings")
        port_id = _validate_port_id(raw_key)
        label = raw_label.strip()[:64]
        if label:
            result[port_id] = label
    return result


def _validate_bool(value: Any, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise SettingsValidationError(f"{field_name} must be a boolean")
    return value


class SettingsStore:
    """Manages loading, validating, and atomically saving PluginSettings."""

    def __init__(
        self,
        config_path: Path,
        defaults: Optional[dict[str, Any]] = None,
    ) -> None:
        self.config_path = Path(config_path)
        self._defaults = dict(defaults or {})
        self._settings = PluginSettings()

    @property
    def settings(self) -> PluginSettings:
        """Return the currently loaded settings instance."""
        return self._settings

    def load(self) -> PluginSettings:
        """Load settings from disk, falling back to defaults when missing."""
        base_data: dict[str, Any] = {}
        if self._defaults:
            base_data.update(self._defaults)

        if self.config_path.exists():
            raw_text = self.config_path.read_text(encoding="utf-8")
            if raw_text.strip():
                loaded = json.loads(raw_text)
                if not isinstance(loaded, dict):
                    raise SettingsValidationError(
                        f"Settings file {self.config_path} must contain a JSON object"
                    )
                base_data.update(loaded)

        self._settings = self._parse_dict(base_data, strict=False)
        if not self.config_path.exists():
            self.save(self._settings)
        return self._settings

    def save(self, settings: Optional[PluginSettings] = None) -> PluginSettings:
        """Atomically persist settings to config_path."""
        target = settings if settings is not None else self._settings
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.config_path.with_suffix(f"{self.config_path.suffix}.tmp")
        payload = json.dumps(target.to_dict(), indent=2, sort_keys=True) + "\n"
        tmp_path.write_text(payload, encoding="utf-8")
        os.replace(str(tmp_path), str(self.config_path))
        self._settings = target
        return target

    def update(self, patch: dict[str, Any]) -> PluginSettings:
        """Validate a partial or full settings update and persist atomically."""
        if not isinstance(patch, dict):
            raise SettingsValidationError("Settings update payload must be a JSON object")
        merged = self._settings.to_dict()
        merged.update(patch)
        validated = self._parse_dict(merged, strict=True)
        return self.save(validated)

    def is_port_allowed(self, port_id: str) -> bool:
        """Check whether port_id is allowed under the current settings."""
        return self._settings.is_port_allowed(port_id)

    def _parse_dict(self, data: dict[str, Any], *, strict: bool) -> PluginSettings:
        defaults = PluginSettings()

        def pick(key: str, validator: Any, default_val: Any) -> Any:
            if key not in data:
                return default_val
            try:
                return validator(data[key])
            except SettingsValidationError:
                if strict:
                    raise
                return default_val

        return PluginSettings(
            port_policy=pick("port_policy", _validate_policy, defaults.port_policy),
            allowed_ports=pick("allowed_ports", _validate_allowed_ports, defaults.allowed_ports),
            port_labels=pick("port_labels", _validate_port_labels, defaults.port_labels),
            max_scan_depth=pick("max_scan_depth", _clamp_scan_depth, defaults.max_scan_depth),
            wait_for_metadata=pick(
                "wait_for_metadata",
                lambda v: _validate_bool(v, "wait_for_metadata"),
                defaults.wait_for_metadata,
            ),
            sync_timeout_seconds=pick(
                "sync_timeout_seconds",
                _clamp_timeout,
                defaults.sync_timeout_seconds,
            ),
            gcode_extensions=pick(
                "gcode_extensions",
                _validate_extensions,
                list(DEFAULT_EXTENSIONS),
            ),
            lcd_notifications=pick(
                "lcd_notifications",
                lambda v: _validate_bool(v, "lcd_notifications"),
                defaults.lcd_notifications,
            ),
            usb_subdir_name=pick(
                "usb_subdir_name",
                _validate_subdir_name,
                defaults.usb_subdir_name,
            ),
        )
