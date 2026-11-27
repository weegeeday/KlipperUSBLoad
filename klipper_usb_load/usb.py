"""Sysfs USB storage discovery, read-only mounting, scanning, and symlink management."""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import shutil
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Protocol

_DEVNODE_RE = re.compile(r"^/dev/sd[a-z]+[0-9]*$")
_PORT_ID_RE = re.compile(r"^[A-Za-z0-9._:-]+$")
_USB_PORT_SEGMENT_RE = re.compile(r"^(\d+)-(\d+(?:\.\d+)*)$")
_BLOCK_DEV_RE = re.compile(r"^(sd[a-z]+)([0-9]*)$")
_SAFE_PORT_CHARS_RE = re.compile(r"[^A-Za-z0-9._-]")

_IGNORED_NAMES = {
    "system volume information",
    "$recycle.bin",
    "__macosx",
}


class MountError(RuntimeError):
    """Raised when mounting, unmounting, or validating a USB storage device fails."""


@dataclass(frozen=True)
class ConnectedUsbStorage:
    """Represents a connected USB block storage device mapped to a physical USB port."""

    port_id: str
    devnode: str
    display_name: str = ""


@dataclass(frozen=True)
class ScannedGcodeFile:
    """Represents a valid G-code file discovered on a mounted USB drive."""

    rel_path: str
    usb_abs_path: str
    display_name: str
    drive_port_id: str
    mtime: float
    size: int


class MountCommandRunner(Protocol):
    """Protocol for executing privileged mount/unmount operations."""

    async def mount(self, devnode: str, mount_point: Path) -> None:
        """Mount devnode read-only at mount_point."""

    async def unmount(self, mount_point: Path, *, force: bool = False) -> None:
        """Unmount mount_point."""


class SubprocessMountRunner:
    """Default MountCommandRunner invoking klipper-usb-load-helper via sudo."""

    def __init__(
        self,
        helper_path: str = "/usr/local/sbin/klipper-usb-load-helper",
        use_sudo: bool = True,
    ) -> None:
        self.helper_path = helper_path
        self.use_sudo = use_sudo

    async def mount(self, devnode: str, mount_point: Path) -> None:
        """Invoke helper script to mount devnode read-only."""
        await self._run("mount", devnode, str(mount_point))

    async def unmount(self, mount_point: Path, *, force: bool = False) -> None:
        """Invoke helper script to unmount mount_point."""
        args = ["unmount", str(mount_point)]
        if force:
            args.append("--force")
        await self._run(*args)

    async def _run(self, *args: str) -> None:
        cmd = (
            ["sudo", "-n", self.helper_path, *args] if self.use_sudo else [self.helper_path, *args]
        )
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            detail = (stderr or stdout).decode("utf-8", errors="replace").strip()
            raise MountError(
                f"Mount helper failed (exit {proc.returncode}) for {args}: {detail or 'no output'}"
            )


def validate_devnode(devnode: str) -> str:
    """Validate that devnode is a safe /dev/sd* block path."""
    cleaned = devnode.strip()
    if not _DEVNODE_RE.match(cleaned):
        raise MountError(f"Unsupported or unsafe device node: {devnode!r}")
    return cleaned


def validate_port_id(port_id: str) -> str:
    """Validate that port_id contains no path traversal or unsafe characters."""
    cleaned = port_id.strip()
    if not cleaned or ".." in cleaned or "/" in cleaned or "\\" in cleaned:
        raise MountError(f"Invalid USB port ID: {port_id!r}")
    if not _PORT_ID_RE.match(cleaned):
        raise MountError(f"Invalid USB port ID characters: {port_id!r}")
    return cleaned


def sanitize_port_id(port_id: str) -> str:
    """Convert a physical USB port ID (e.g. '1-1.3') into a safe folder/mount name."""
    cleaned = _SAFE_PORT_CHARS_RE.sub("_", port_id.strip())
    if not cleaned or cleaned in (".", ".."):
        raise MountError(f"Cannot sanitize invalid port_id: {port_id!r}")
    return cleaned


def extract_port_id_from_sysfs_path(path_text: str) -> str | None:
    """Extract the deepest USB port ID segment (e.g. '1-1.3') from a sysfs path."""
    parts = [segment for segment in path_text.replace("\\", "/").split("/") if segment]
    found: str | None = None
    for segment in parts:
        if ":" in segment:
            continue
        if _USB_PORT_SEGMENT_RE.match(segment):
            found = segment
    return found


def _resolve_block_port(block_entry: Path) -> str | None:
    candidates: list[str] = []
    marker = block_entry / "device_path"
    if marker.is_file():
        with contextlib.suppress(OSError):
            candidates.append(marker.read_text(encoding="utf-8", errors="replace").strip())
    if block_entry.is_symlink():
        with contextlib.suppress(OSError):
            candidates.append(os.readlink(block_entry))
    with contextlib.suppress(OSError):
        candidates.append(str(block_entry.resolve()))
    device_link = block_entry / "device"
    if device_link.exists() or device_link.is_symlink():
        with contextlib.suppress(OSError):
            candidates.append(str(device_link.resolve()))

    for path_text in candidates:
        port_id = extract_port_id_from_sysfs_path(path_text)
        if port_id:
            return port_id
    return None


_USB_FOLDER_MARKER = ".klipper_usb_load"
_UNSAFE_FOLDER_CHARS_RE = re.compile(r"[^A-Za-z0-9._-]+")


def read_usb_product_name(sysfs_root: Path, port_id: str) -> str:
    """Read the USB hardware product string from sysfs for port_id, if present."""
    product_path = Path(sysfs_root) / "bus" / "usb" / "devices" / port_id / "product"
    if not product_path.is_file():
        return ""
    try:
        return product_path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def _decode_udev_label(raw_name: str) -> str:
    return re.sub(r"\\x([0-9a-fA-F]{2})", lambda m: chr(int(m.group(1), 16)), raw_name).strip()


def resolve_volume_label(
    devnode: str,
    by_label_dir: Path = Path("/dev/disk/by-label"),
) -> str:
    """Look up the filesystem volume label for devnode from /dev/disk/by-label."""
    if not devnode or not by_label_dir.is_dir():
        return ""
    try:
        target_resolved = str(Path(devnode).resolve())
    except OSError:
        target_resolved = devnode

    try:
        entries = sorted(by_label_dir.iterdir(), key=lambda p: p.name)
    except OSError:
        return ""

    for entry in entries:
        try:
            resolved = str(entry.resolve())
        except OSError:
            continue
        if resolved in (target_resolved, devnode):
            return _decode_udev_label(entry.name)
    return ""


def resolve_usb_display_name(
    port_id: str,
    *,
    fs_label: str = "",
    devnode: str = "",
    sysfs_root: Path = Path("/sys"),
    by_label_dir: Path = Path("/dev/disk/by-label"),
) -> str:
    """Resolve the best human-friendly name: volume label -> USB product -> port_id."""
    cleaned_label = fs_label.strip()
    if cleaned_label:
        return cleaned_label
    vol_label = resolve_volume_label(devnode, by_label_dir=by_label_dir)
    if vol_label:
        return vol_label
    product = read_usb_product_name(sysfs_root, port_id)
    if product:
        return product
    return port_id


def sanitize_drive_folder_name(
    display_name: str,
    port_id: str,
    existing_folders: set[str] | None = None,
) -> str:
    """Turn a USB display name into a safe, collision-free folder name under gcodes/usb/."""
    fallback = f"port_{sanitize_port_id(port_id)}"
    cleaned = _UNSAFE_FOLDER_CHARS_RE.sub("_", display_name.strip()).strip("._-")
    cleaned = fallback if not cleaned else cleaned[:32].rstrip("._-") or fallback

    used = existing_folders or set()
    if cleaned in used:
        suffix = f"_{sanitize_port_id(port_id)}"
        base = cleaned[: max(1, 32 - len(suffix))].rstrip("._-")
        cleaned = f"{base}{suffix}"
    return cleaned


def scan_connected_storage_ports(
    sysfs_root: Path = Path("/sys"),
    by_label_dir: Path = Path("/dev/disk/by-label"),
) -> list[ConnectedUsbStorage]:
    """Return connected USB block storage devices mapped to their physical USB port ID."""
    block_dir = Path(sysfs_root) / "class" / "block"
    if not block_dir.is_dir():
        return []

    per_port: dict[str, list[tuple[str, bool]]] = {}
    try:
        entries = sorted(block_dir.iterdir(), key=lambda p: p.name)
    except OSError:
        return []

    for entry in entries:
        match = _BLOCK_DEV_RE.match(entry.name)
        if not match:
            continue
        is_partition = bool(match.group(2))
        port_id = _resolve_block_port(entry)
        if not port_id:
            continue
        per_port.setdefault(port_id, []).append((f"/dev/{entry.name}", is_partition))

    discovered: list[ConnectedUsbStorage] = []
    for port_id in sorted(per_port.keys()):
        candidates = per_port[port_id]
        partitions = [dev for dev, is_part in candidates if is_part]
        devnode = sorted(partitions)[0] if partitions else sorted(d for d, _ in candidates)[0]
        display_name = resolve_usb_display_name(
            port_id,
            devnode=devnode,
            sysfs_root=sysfs_root,
            by_label_dir=by_label_dir,
        )
        discovered.append(
            ConnectedUsbStorage(port_id=port_id, devnode=devnode, display_name=display_name)
        )
    return discovered


def _should_ignore_name(name: str) -> bool:
    return name.startswith(".") or name.lower() in _IGNORED_NAMES


class UsbMountManager:
    """Coordinates read-only mounts, G-code scanning, and per-port symlink folders."""

    def __init__(
        self,
        gcodes_dir: Path,
        mount_base: Path = Path("/run/klipper-usb-load"),
        usb_subdir_name: str = "usb",
        runner: MountCommandRunner | None = None,
        link_fn: Callable[[str, str], None] | None = None,
    ) -> None:
        self.gcodes_dir = Path(gcodes_dir)
        self.mount_base = Path(mount_base)
        self.usb_subdir_name = usb_subdir_name
        self.runner: MountCommandRunner = runner if runner is not None else SubprocessMountRunner()
        self._link_fn: Callable[[str, str], None] = link_fn if link_fn is not None else os.symlink

    @property
    def usb_root_dir(self) -> Path:
        """Return the root folder inside gcodes/ holding all USB port subfolders."""
        return self.gcodes_dir / self.usb_subdir_name

    def port_folder_name(self, port_id: str) -> str:
        """Return the deterministic subfolder name for a physical USB port."""
        validate_port_id(port_id)
        return f"port_{sanitize_port_id(port_id)}"

    def port_mount_point(self, port_id: str) -> Path:
        """Return the host mount point path for a physical USB port."""
        validate_port_id(port_id)
        return self.mount_base / sanitize_port_id(port_id)

    def port_gcode_dir(self, port_id: str, folder_name: str | None = None) -> Path:
        """Return the gcodes subfolder path for a mounted USB drive."""
        resolved = folder_name or self.port_folder_name(port_id)
        return self.usb_root_dir / resolved

    def port_gcode_rel_dir(self, port_id: str, folder_name: str | None = None) -> str:
        """Return the POSIX relative path from gcodes/ to this drive's folder."""
        resolved = folder_name or self.port_folder_name(port_id)
        return str(PurePosixPath(self.usb_subdir_name) / resolved)

    async def mount_drive(self, devnode: str, port_id: str) -> Path:
        """Validate inputs and mount devnode read-only at this port's mount point."""
        valid_dev = validate_devnode(devnode)
        valid_port = validate_port_id(port_id)
        mount_point = self.port_mount_point(valid_port)
        await self.runner.mount(valid_dev, mount_point)
        return mount_point

    async def unmount_drive(
        self,
        port_id: str,
        *,
        folder_name: str | None = None,
        force: bool = False,
    ) -> None:
        """Remove this drive's symlink directory and unmount the USB device."""
        valid_port = validate_port_id(port_id)
        self.remove_port_symlinks(valid_port, folder_name=folder_name)
        mount_point = self.port_mount_point(valid_port)
        await self.runner.unmount(mount_point, force=force)

    def remove_port_symlinks(self, port_id: str, folder_name: str | None = None) -> None:
        """Delete only the symlink tree belonging to this USB drive."""
        target_dir = self.port_gcode_dir(port_id, folder_name=folder_name)
        if target_dir.exists() or target_dir.is_symlink():
            shutil.rmtree(target_dir, ignore_errors=False)

    def cleanup_stale_links(self, active_entries: set[str]) -> list[str]:
        """Remove orphaned USB symlink folders inside gcodes/<usb_subdir> on startup."""
        root = self.usb_root_dir
        if not root.is_dir():
            return []

        active_folders = set(active_entries)
        for item in active_entries:
            with contextlib.suppress(MountError):
                active_folders.add(self.port_folder_name(item))

        removed: list[str] = []
        for entry in list(root.iterdir()):
            if entry.name in active_folders:
                continue
            is_managed = entry.name.startswith("port_") or (
                entry.is_dir() and (entry / _USB_FOLDER_MARKER).is_file()
            )
            if not is_managed:
                continue
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry, ignore_errors=True)
            else:
                entry.unlink(missing_ok=True)
            removed.append(entry.name)
        return removed

    def scan_mount_for_gcodes(
        self,
        mount_point: Path,
        port_id: str,
        *,
        folder_name: str | None = None,
        max_depth: int = 2,
        extensions: Sequence[str] = (".gcode", ".gco", ".g"),
    ) -> list[ScannedGcodeFile]:
        """Walk mount_point up to max_depth and return discovered G-code files."""
        validate_port_id(port_id)
        root = Path(mount_point)
        if not root.is_dir():
            return []

        resolved_root = root.resolve()
        allowed_exts = {ext.lower() for ext in extensions}
        resolved_folder = folder_name or self.port_folder_name(port_id)
        base_rel = PurePosixPath(self.usb_subdir_name) / resolved_folder
        results: list[ScannedGcodeFile] = []

        def walk_dir(current: Path, rel_parts: tuple[str, ...], depth: int) -> None:
            try:
                entries = sorted(current.iterdir(), key=lambda p: p.name.lower())
            except OSError:
                return

            for entry in entries:
                name = entry.name
                if _should_ignore_name(name):
                    continue

                if entry.is_symlink():
                    try:
                        resolved_entry = entry.resolve(strict=True)
                        resolved_entry.relative_to(resolved_root)
                    except (OSError, ValueError):
                        continue

                if entry.is_dir():
                    if depth < max_depth:
                        walk_dir(entry, rel_parts + (name,), depth + 1)
                    continue

                if not entry.is_file() or entry.suffix.lower() not in allowed_exts:
                    continue

                try:
                    stat_result = entry.stat()
                except OSError:
                    continue
                if stat_result.st_size <= 0:
                    continue

                posix_rel = str(base_rel.joinpath(*rel_parts, name))
                results.append(
                    ScannedGcodeFile(
                        rel_path=posix_rel,
                        usb_abs_path=str(entry),
                        display_name=name,
                        drive_port_id=port_id,
                        mtime=float(stat_result.st_mtime),
                        size=int(stat_result.st_size),
                    )
                )

        walk_dir(root, (), 0)
        return results

    def materialize_symlinks(
        self,
        port_id: str,
        files: Sequence[ScannedGcodeFile],
        *,
        folder_name: str | None = None,
    ) -> Path:
        """Rebuild gcodes/<usb_subdir>/<folder_name>/ with symlinks for all scanned files."""
        validate_port_id(port_id)
        resolved_folder = folder_name or self.port_folder_name(port_id)
        self.usb_root_dir.mkdir(parents=True, exist_ok=True)
        port_dir = self.port_gcode_dir(port_id, folder_name=resolved_folder)
        if port_dir.exists():
            shutil.rmtree(port_dir)
        port_dir.mkdir(parents=True, exist_ok=True)
        (port_dir / _USB_FOLDER_MARKER).write_text(f"{port_id}\n", encoding="utf-8")

        port_rel_prefix = PurePosixPath(self.usb_subdir_name) / resolved_folder
        for item in files:
            rel_inside_port = PurePosixPath(item.rel_path).relative_to(port_rel_prefix)
            link_path = port_dir.joinpath(*rel_inside_port.parts)
            link_path.parent.mkdir(parents=True, exist_ok=True)
            if link_path.exists() or link_path.is_symlink():
                link_path.unlink()
            self._link_fn(item.usb_abs_path, str(link_path))

        return port_dir
