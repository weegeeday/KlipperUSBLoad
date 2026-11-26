"""Data models for KlipperUSBLoad."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal, Optional

PortPolicy = Literal["allow_all", "allowlist"]
DeviceClass = Literal["storage", "non_storage", "empty"]
SyncState = Literal["idle", "scanning", "syncing", "ready", "warning", "error"]


@dataclass
class UsbPortInfo:
    """Represents a physical USB port and any device currently attached to it."""

    port_id: str
    bus_num: int
    port_path: str
    label: str = ""
    allowed: bool = True
    connected: bool = False
    device_class: DeviceClass = "empty"
    vendor: str = ""
    product: str = ""
    serial: str = ""
    devnode: str = ""
    fs_label: str = ""
    fs_type: str = ""
    mounted: bool = False
    mount_point: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serialize port information to a JSON-compatible dictionary."""
        return asdict(self)


@dataclass
class ScannedGcodeFile:
    """Represents a G-code file discovered on a mounted USB flash drive."""

    rel_path: str
    usb_abs_path: str
    display_name: str
    drive_port_id: str
    mtime: float
    size: int

    def to_dict(self) -> dict[str, Any]:
        """Serialize scanned file metadata to a JSON-compatible dictionary."""
        return asdict(self)


@dataclass
class SyncProgress:
    """Tracks Moonraker indexing progress for a mounted USB drive."""

    state: SyncState = "idle"
    total_files: int = 0
    seen_files: int = 0
    metadata_ready_files: int = 0
    missing_files: list[str] = field(default_factory=list)
    message: str = ""
    warning: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serialize sync progress to a JSON-compatible dictionary."""
        return asdict(self)


@dataclass
class MountedDriveState:
    """Runtime state for an actively mounted USB drive."""

    port_id: str
    devnode: str
    mount_point: str
    gcode_subdir: str
    fs_label: str = ""
    fs_type: str = ""
    files: list[ScannedGcodeFile] = field(default_factory=list)
    sync_progress: SyncProgress = field(default_factory=SyncProgress)
    mounted_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        """Serialize mounted drive state to a JSON-compatible dictionary."""
        return {
            "port_id": self.port_id,
            "devnode": self.devnode,
            "mount_point": self.mount_point,
            "gcode_subdir": self.gcode_subdir,
            "fs_label": self.fs_label,
            "fs_type": self.fs_type,
            "file_count": len(self.files),
            "files": [item.to_dict() for item in self.files],
            "sync_progress": self.sync_progress.to_dict(),
            "mounted_at": self.mounted_at,
        }


@dataclass
class ActivePrintInfo:
    """Minimal snapshot of Klipper's current print job state."""

    state: str = "standby"
    filename: str = ""

    @property
    def is_active(self) -> bool:
        """Return True if a print is actively running or paused."""
        return self.state.lower() in ("printing", "paused")

    def to_dict(self) -> dict[str, Any]:
        """Serialize active print status to a JSON-compatible dictionary."""
        return {
            "state": self.state,
            "filename": self.filename,
            "is_active": self.is_active,
        }


@dataclass
class PluginSettings:
    """User-configurable KlipperUSBLoad settings persisted to JSON."""

    port_policy: PortPolicy = "allow_all"
    allowed_ports: list[str] = field(default_factory=list)
    port_labels: dict[str, str] = field(default_factory=dict)
    max_scan_depth: int = 2
    wait_for_metadata: bool = True
    sync_timeout_seconds: float = 60.0
    gcode_extensions: list[str] = field(default_factory=lambda: [".gcode", ".gco", ".g"])
    lcd_notifications: bool = True
    usb_subdir_name: str = "usb"

    def is_port_allowed(self, port_id: str) -> bool:
        """Return True if the given physical USB port is permitted by current policy."""
        if self.port_policy == "allow_all":
            return True
        return port_id in self.allowed_ports

    def label_for(self, port_id: str) -> str:
        """Return the custom label for a port if configured, else empty string."""
        return self.port_labels.get(port_id, "")

    def to_dict(self) -> dict[str, Any]:
        """Serialize settings to a JSON-compatible dictionary."""
        return asdict(self)


@dataclass
class DetectModeState:
    """Tracks interactive 'plug in a drive to identify port' mode."""

    active: bool = False
    expires_at: float = 0.0
    detected_port_id: Optional[str] = None
    detected_device: Optional[dict[str, Any]] = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize detect-mode state to a JSON-compatible dictionary."""
        return asdict(self)
