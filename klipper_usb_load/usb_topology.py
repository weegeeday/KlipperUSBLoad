"""Physical USB port topology discovery via Linux sysfs."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Optional

from .models import DeviceClass, MountedDriveState, PluginSettings, UsbPortInfo

_ROOT_HUB_RE = re.compile(r"^usb(\d+)$")
_USB_PORT_RE = re.compile(r"^(\d+)-(\d+(?:\.\d+)*)$")
_BLOCK_DEV_RE = re.compile(r"^(sd[a-z]+)([0-9]*)$")
_SAFE_PORT_CHARS_RE = re.compile(r"[^A-Za-z0-9._-]")

_SYSTEM_ROOT_HUB_CLASS = "09"
_USB_STORAGE_CLASS = "08"


def sanitize_port_id(port_id: str) -> str:
    """Convert a physical USB port ID (e.g. '1-1.3') into a safe directory/mount name."""
    cleaned = _SAFE_PORT_CHARS_RE.sub("_", port_id.strip())
    if not cleaned or cleaned in (".", ".."):
        raise ValueError(f"Cannot sanitize invalid port_id: {port_id!r}")
    return cleaned


def port_sort_key(port_id: str) -> tuple[int, tuple[int, ...], str]:
    """Return a deterministic numeric sort key for USB port IDs like '1-1.2'."""
    match = _USB_PORT_RE.match(port_id)
    if not match:
        return (9999, (), port_id)
    bus = int(match.group(1))
    chain = tuple(int(part) for part in match.group(2).split("."))
    return (bus, chain, port_id)


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def _read_int(path: Path, default: int = 0) -> int:
    raw = _read_text(path)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def extract_port_id_from_sysfs_path(path_text: str) -> Optional[str]:
    """Extract the deepest USB port ID segment (e.g. '1-1.3') from a sysfs device path."""
    parts = [segment for segment in path_text.replace("\\", "/").split("/") if segment]
    found: Optional[str] = None
    for segment in parts:
        if ":" in segment:
            continue
        if _USB_PORT_RE.match(segment):
            found = segment
    return found


class UsbTopologyScanner:
    """Discovers physical USB ports, attached devices, and block device mappings from sysfs."""

    def __init__(self, sysfs_root: Path = Path("/sys")) -> None:
        self.sysfs_root = Path(sysfs_root)

    def scan_ports(
        self,
        settings: Optional[PluginSettings] = None,
        mounted_drives: Optional[dict[str, MountedDriveState]] = None,
    ) -> list[UsbPortInfo]:
        """Scan sysfs and merge with configured labels, allowlist, and mounted drives."""
        usb_devices_dir = self.sysfs_root / "bus" / "usb" / "devices"
        block_map = self._scan_block_devices()

        discovered: dict[str, UsbPortInfo] = {}
        hub_ports: set[str] = set()
        expected_child_ports: set[str] = set()

        if usb_devices_dir.is_dir():
            entries = sorted(usb_devices_dir.iterdir(), key=lambda p: p.name)
            for entry in entries:
                name = entry.name
                root_match = _ROOT_HUB_RE.match(name)
                if root_match:
                    bus_num = int(root_match.group(1))
                    maxchild = _read_int(entry / "maxchild", 0)
                    for idx in range(1, maxchild + 1):
                        expected_child_ports.add(f"{bus_num}-{idx}")
                    continue

                if ":" in name:
                    continue

                port_match = _USB_PORT_RE.match(name)
                if not port_match:
                    continue

                bus_num = int(port_match.group(1))
                port_path = port_match.group(2)
                device_class = self._classify_usb_device(entry, usb_devices_dir)

                if self._is_hub_device(entry, usb_devices_dir):
                    hub_ports.add(name)
                    maxchild = _read_int(entry / "maxchild", 0)
                    for idx in range(1, maxchild + 1):
                        expected_child_ports.add(f"{name}.{idx}")
                    continue

                devnode = block_map.get(name, "")
                if devnode and device_class == "non_storage":
                    device_class = "storage"

                discovered[name] = UsbPortInfo(
                    port_id=name,
                    bus_num=bus_num,
                    port_path=port_path,
                    connected=True,
                    device_class=device_class,
                    vendor=_read_text(entry / "manufacturer"),
                    product=_read_text(entry / "product"),
                    serial=_read_text(entry / "serial"),
                    devnode=devnode,
                )

        for empty_port in sorted(expected_child_ports):
            if empty_port in discovered or empty_port in hub_ports:
                continue
            port_match = _USB_PORT_RE.match(empty_port)
            if not port_match:
                continue
            discovered[empty_port] = UsbPortInfo(
                port_id=empty_port,
                bus_num=int(port_match.group(1)),
                port_path=port_match.group(2),
                connected=False,
                device_class="empty",
            )

        return merge_known_ports(discovered, settings=settings, mounted_drives=mounted_drives)

    def _is_hub_device(self, device_dir: Path, usb_devices_dir: Path) -> bool:
        if _read_text(device_dir / "bDeviceClass").lower() == _SYSTEM_ROOT_HUB_CLASS:
            return True
        return _SYSTEM_ROOT_HUB_CLASS in self._read_interface_classes(device_dir, usb_devices_dir)

    def _classify_usb_device(
        self,
        device_dir: Path,
        usb_devices_dir: Path,
    ) -> DeviceClass:
        classes = self._read_interface_classes(device_dir, usb_devices_dir)
        if _USB_STORAGE_CLASS in classes:
            return "storage"
        return "non_storage"

    def _read_interface_classes(self, device_dir: Path, usb_devices_dir: Path) -> set[str]:
        classes: set[str] = set()
        port_prefix = f"{device_dir.name}:"

        direct_iface = _read_text(device_dir / "bInterfaceClass").lower()
        if direct_iface:
            classes.add(direct_iface)

        for candidate_root in (device_dir, usb_devices_dir):
            if not candidate_root.is_dir():
                continue
            try:
                children = list(candidate_root.iterdir())
            except OSError:
                continue
            for child in children:
                if not child.is_dir():
                    continue
                if not (
                    child.name.startswith(port_prefix)
                    or (candidate_root == device_dir and child.name.startswith("iface_"))
                ):
                    continue
                iface_class = _read_text(child / "bInterfaceClass").lower()
                if iface_class:
                    classes.add(iface_class)
        return classes

    def _scan_block_devices(self) -> dict[str, str]:
        """Map physical USB port_id -> preferred /dev/sd* device node."""
        block_dir = self.sysfs_root / "class" / "block"
        if not block_dir.is_dir():
            return {}

        per_port_disks: dict[str, list[tuple[str, bool]]] = {}
        try:
            entries = sorted(block_dir.iterdir(), key=lambda p: p.name)
        except OSError:
            return {}

        for entry in entries:
            match = _BLOCK_DEV_RE.match(entry.name)
            if not match:
                continue
            is_partition = bool(match.group(2))
            port_id = self._resolve_block_port(entry)
            if not port_id:
                continue
            per_port_disks.setdefault(port_id, []).append((f"/dev/{entry.name}", is_partition))

        selected: dict[str, str] = {}
        for port_id, candidates in per_port_disks.items():
            partitions = [dev for dev, is_part in candidates if is_part]
            if partitions:
                selected[port_id] = sorted(partitions)[0]
            else:
                selected[port_id] = sorted(dev for dev, _ in candidates)[0]
        return selected

    def _resolve_block_port(self, block_entry: Path) -> Optional[str]:
        candidates: list[str] = []
        # Optional text marker for cross-platform tests where symlinks are unavailable
        marker = _read_text(block_entry / "device_path")
        if marker:
            candidates.append(marker)
        if block_entry.is_symlink():
            try:
                candidates.append(os.readlink(block_entry))
            except OSError:
                pass
        try:
            candidates.append(str(block_entry.resolve()))
        except OSError:
            pass

        device_link = block_entry / "device"
        if device_link.exists() or device_link.is_symlink():
            try:
                candidates.append(str(device_link.resolve()))
            except OSError:
                pass

        for path_text in candidates:
            port_id = extract_port_id_from_sysfs_path(path_text)
            if port_id:
                return port_id
        return None


def merge_known_ports(
    discovered: dict[str, UsbPortInfo],
    settings: Optional[PluginSettings] = None,
    mounted_drives: Optional[dict[str, MountedDriveState]] = None,
) -> list[UsbPortInfo]:
    """Merge live sysfs ports with configured labels/allowlist and active mounts."""
    ports: dict[str, UsbPortInfo] = dict(discovered)
    drives = mounted_drives or {}

    known_ids: set[str] = set(ports.keys()) | set(drives.keys())
    if settings is not None:
        known_ids |= set(settings.allowed_ports)
        known_ids |= set(settings.port_labels.keys())

    for port_id in known_ids:
        if port_id not in ports:
            match = _USB_PORT_RE.match(port_id)
            bus_num = int(match.group(1)) if match else 0
            port_path = match.group(2) if match else port_id
            ports[port_id] = UsbPortInfo(
                port_id=port_id,
                bus_num=bus_num,
                port_path=port_path,
                connected=port_id in drives,
                device_class="storage" if port_id in drives else "empty",
            )

        info = ports[port_id]
        if settings is not None:
            info.allowed = settings.is_port_allowed(port_id)
            info.label = settings.label_for(port_id)
        if port_id in drives:
            drive = drives[port_id]
            info.connected = True
            info.device_class = "storage"
            info.mounted = True
            info.mount_point = drive.mount_point
            if drive.devnode and not info.devnode:
                info.devnode = drive.devnode
            info.fs_label = drive.fs_label
            info.fs_type = drive.fs_type

    return [ports[pid] for pid in sorted(ports.keys(), key=port_sort_key)]
