"""Moonraker component for KlipperUSBLoad (INI-configured, LCD-only)."""

from __future__ import annotations

import asyncio
import hmac
import inspect
import logging
import re
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .config import UsbLoadConfig
from .sync import GcodeSyncEngine, SyncProgress, select_newest_file
from .usb import (
    MountError,
    ScannedGcodeFile,
    UsbMountManager,
    scan_connected_storage_ports,
    validate_devnode,
    validate_port_id,
)

LOGGER = logging.getLogger("moonraker.klipper_usb_load")

_GCODE_CTRL_RE = re.compile(r"[\r\n\x00]+")


def sanitize_gcode_string(value: str, max_len: int = 180) -> str:
    """Strip control chars and double quotes so values are safe inside G-code strings."""
    cleaned = _GCODE_CTRL_RE.sub(" ", value).replace('"', "'").strip()
    return cleaned[:max_len]


def _resolve_request_types(methods: list[str]) -> Any:
    try:
        from moonraker.common import RequestType  # type: ignore[import-not-found]
    except ImportError:
        return methods

    resolved: Any = None
    for method in methods:
        enum_val = getattr(RequestType, method.upper(), None)
        if enum_val is None:
            return methods
        resolved = enum_val if resolved is None else (resolved | enum_val)
    return resolved


@dataclass
class MountedDrive:
    """Runtime state for an actively mounted USB drive."""

    port_id: str
    devnode: str
    gcode_subdir: str
    files: list[ScannedGcodeFile] = field(default_factory=list)
    ready_paths: set[str] = field(default_factory=set)


class MoonrakerBridge:
    """Adapts Moonraker's file_manager and klippy_apis for KlipperUSBLoad."""

    def __init__(self, server: Any) -> None:
        self.server = server
        self._macros_checked: bool = False
        self._has_set_state_macro: bool = True
        self._has_notify_macro: bool = True
        self._warned_missing_macros: bool = False

    def _get_component(self, name: str) -> Any:
        try:
            return self.server.lookup_component(name, None)
        except TypeError:
            try:
                return self.server.lookup_component(name)
            except Exception:
                return None
        except Exception:
            return None

    def reset_macro_cache(self) -> None:
        """Clear cached Klipper macro presence flags when Klippy reconnects."""
        self._macros_checked = False
        self._warned_missing_macros = False

    async def refresh_directory(self, rel_dir: str) -> None:
        """Ask Moonraker file_manager to rescan rel_dir inside gcodes/."""
        file_manager = self._get_component("file_manager")
        if file_manager is None:
            return

        for method_name in ("refresh_directory", "sync_dir", "schedule_sync_dir"):
            method = getattr(file_manager, method_name, None)
            if not callable(method):
                continue
            try:
                result = method("gcodes", rel_dir)
                if inspect.isawaitable(result):
                    await result
                return
            except TypeError:
                try:
                    result = method(f"gcodes/{rel_dir}")
                    if inspect.isawaitable(result):
                        await result
                    return
                except Exception:
                    continue
            except Exception as err:
                LOGGER.debug("Moonraker %s(%s) raised: %s", method_name, rel_dir, err)

    async def list_gcode_files(self) -> dict[str, dict[str, Any]]:
        """Return Moonraker's current gcodes file mapping (rel_path -> info dict)."""
        file_manager = self._get_component("file_manager")
        if file_manager is None:
            return {}

        get_file_list = getattr(file_manager, "get_file_list", None)
        if not callable(get_file_list):
            return {}

        try:
            raw = get_file_list("gcodes")
            if inspect.isawaitable(raw):
                raw = await raw
        except TypeError:
            raw = get_file_list("gcodes", list_format=False)
            if inspect.isawaitable(raw):
                raw = await raw
        except Exception as err:
            LOGGER.debug("file_manager.get_file_list failed: %s", err)
            return {}

        if isinstance(raw, dict):
            return {str(k): (v if isinstance(v, dict) else {}) for k, v in raw.items()}
        if isinstance(raw, list):
            return {
                str(item["path"]): item
                for item in raw
                if isinstance(item, dict) and "path" in item
            }
        return {}

    async def is_metadata_ready(self, rel_path: str) -> bool:
        """Return True if Moonraker has metadata stored in memory for rel_path."""
        file_manager = self._get_component("file_manager")
        if file_manager is None:
            return False

        gcode_metadata = getattr(file_manager, "gcode_metadata", None)
        if gcode_metadata is not None:
            for attr in ("get", "get_metadata"):
                getter = getattr(gcode_metadata, attr, None)
                if not callable(getter):
                    continue
                try:
                    meta = getter(rel_path)
                    if inspect.isawaitable(meta):
                        meta = await meta
                    if isinstance(meta, dict) and bool(meta):
                        return True
                except Exception:
                    continue

        get_meta = getattr(file_manager, "get_file_metadata", None)
        if callable(get_meta):
            try:
                meta = get_meta(rel_path)
                if inspect.isawaitable(meta):
                    meta = await meta
                if isinstance(meta, dict) and (
                    "slicer" in meta
                    or "estimated_time" in meta
                    or "metadata_pulled" in meta
                    or "size" in meta
                ):
                    return True
            except Exception:
                pass

        return False

    async def request_metadata_parse(self, rel_path: str) -> None:
        """Trigger Moonraker metadata parsing for rel_path if exposed."""
        file_manager = self._get_component("file_manager")
        if file_manager is None:
            return
        gcode_metadata = getattr(file_manager, "gcode_metadata", None)
        if gcode_metadata is None:
            return

        for method_name in ("parse_metadata", "add_file"):
            method = getattr(gcode_metadata, method_name, None)
            if not callable(method):
                continue
            try:
                result = method(rel_path, {})
                if inspect.isawaitable(result):
                    await result
                return
            except TypeError:
                try:
                    result = method(rel_path)
                    if inspect.isawaitable(result):
                        await result
                    return
                except Exception:
                    continue
            except Exception:
                return

    async def check_macros_installed(self) -> bool:
        """Verify whether _USB_LOAD_SET_STATE and _USB_LOAD_NOTIFY exist in Klipper."""
        if self._macros_checked:
            return self._has_set_state_macro

        klippy_apis = self._get_component("klippy_apis")
        if klippy_apis is None:
            return self._has_set_state_macro

        get_object_list = getattr(klippy_apis, "get_object_list", None)
        if not callable(get_object_list):
            self._macros_checked = True
            self._has_set_state_macro = True
            self._has_notify_macro = True
            return True

        try:
            objects = get_object_list()
            if inspect.isawaitable(objects):
                objects = await objects
            if isinstance(objects, (list, tuple, set)):
                obj_set = {str(o) for o in objects}
                self._has_set_state_macro = "gcode_macro _USB_LOAD_SET_STATE" in obj_set
                self._has_notify_macro = "gcode_macro _USB_LOAD_NOTIFY" in obj_set
                self._macros_checked = True
                if not self._has_set_state_macro and not self._warned_missing_macros:
                    self._warned_missing_macros = True
                    LOGGER.warning(
                        "Klipper macros not loaded in printer.cfg. Add "
                        "[include klipper_usb_load.cfg] and restart Klipper."
                    )
        except Exception as err:
            LOGGER.debug("get_object_list() unavailable: %s", err)

        return self._has_set_state_macro

    async def get_active_print_filename(self) -> Optional[str]:
        """Return the filename being printed/paused in Klipper, or None if idle."""
        klippy_apis = self._get_component("klippy_apis")
        if klippy_apis is None:
            return None
        query_objects = getattr(klippy_apis, "query_objects", None)
        if not callable(query_objects):
            return None

        try:
            result = query_objects({"print_stats": ["state", "filename"]})
            if inspect.isawaitable(result):
                result = await result
            if isinstance(result, dict):
                stats = result.get("print_stats", {})
                if isinstance(stats, dict):
                    state = str(stats.get("state", "standby")).lower()
                    filename = str(stats.get("filename", ""))
                    if state in ("printing", "paused") and filename:
                        return filename
        except Exception as err:
            LOGGER.debug("Could not query Klipper print_stats: %s", err)
        return None

    async def run_gcode(self, script: str) -> None:
        """Execute a G-code script on Klipper via klippy_apis."""
        klippy_apis = self._get_component("klippy_apis")
        if klippy_apis is None:
            return
        run_gcode = getattr(klippy_apis, "run_gcode", None)
        if not callable(run_gcode):
            return
        try:
            result = run_gcode(script)
            if inspect.isawaitable(result):
                await result
        except Exception as err:
            LOGGER.debug("run_gcode(%s) failed: %s", script, err)

    async def publish_state(
        self,
        *,
        newest_file: Optional[ScannedGcodeFile],
        total_count: int,
        status_text: str,
        notify_msg: Optional[str] = None,
        lcd_notifications: bool = True,
        update_macro_state: bool = True,
    ) -> None:
        """Update Klipper _USB_LOAD_STATE variables and optionally display M117 on LCD."""
        await self.check_macros_installed()

        if update_macro_state and self._has_set_state_macro:
            if newest_file is not None:
                filename = sanitize_gcode_string(newest_file.rel_path, max_len=240)
                display_name = sanitize_gcode_string(newest_file.display_name, max_len=64)
                ready = 1
            else:
                filename = ""
                display_name = ""
                ready = 0
            safe_status = sanitize_gcode_string(status_text, max_len=64)
            await self.run_gcode(
                f'_USB_LOAD_SET_STATE FILENAME="{filename}" DISPLAY_NAME="{display_name}" '
                f'READY={ready} COUNT={int(total_count)} STATUS="{safe_status}"'
            )

        if lcd_notifications and notify_msg:
            safe_msg = sanitize_gcode_string(notify_msg, max_len=80)
            if safe_msg:
                if self._has_notify_macro:
                    await self.run_gcode(f'_USB_LOAD_NOTIFY MSG="{safe_msg}"')
                else:
                    await self.run_gcode(f"M117 {safe_msg}")


class KlipperUsbLoad:
    """Moonraker plugin orchestrating USB mounting, full indexing wait, and LCD state."""

    def __init__(
        self,
        config: Any,
        *,
        mount_manager: Optional[UsbMountManager] = None,
        bridge: Optional[MoonrakerBridge] = None,
    ) -> None:
        self.server = config.get_server() if hasattr(config, "get_server") else config["server"]
        self.cfg = UsbLoadConfig.from_moonraker_config(config)
        self._data_path = self._resolve_data_path()

        gcodes_dir = self.cfg.gcodes_path or (self._data_path / "gcodes")
        self.mount_manager = mount_manager or UsbMountManager(
            gcodes_dir=gcodes_dir,
            mount_base=self.cfg.mount_base,
            usb_subdir_name=self.cfg.usb_subdir_name,
        )
        self.bridge = bridge or MoonrakerBridge(self.server)
        self.sync_engine = GcodeSyncEngine(self.bridge)

        self.token_path = self.cfg.udev_token_path or (self.mount_manager.mount_base / "udev.token")
        self._udev_token: str = ""

        self._mounted_drives: dict[str, MountedDrive] = {}
        self._port_locks: dict[str, asyncio.Lock] = {}
        self._port_cancel_events: dict[str, asyncio.Event] = {}
        self._port_generations: dict[str, int] = {}
        self._newest_file: Optional[ScannedGcodeFile] = None

        self._register_endpoints()
        self._register_events()

    def _resolve_data_path(self) -> Path:
        get_app_args = getattr(self.server, "get_app_args", None)
        if callable(get_app_args):
            args = get_app_args()
            if isinstance(args, dict) and args.get("data_path"):
                return Path(str(args["data_path"]))
        return Path.home() / "printer_data"

    def _register_endpoints(self) -> None:
        register_endpoint = getattr(self.server, "register_endpoint", None)
        if callable(register_endpoint):
            register_endpoint(
                "/server/klipper_usb_load/udev-event",
                _resolve_request_types(["POST"]),
                self._handle_udev_event,
            )

    def _register_events(self) -> None:
        register_event = getattr(self.server, "register_event_handler", None)
        if callable(register_event):
            register_event("file_manager:filelist_changed", self._on_filelist_changed)
            register_event("server:klippy_ready", self._on_klippy_ready)

    async def component_init(self) -> None:
        """Moonraker lifecycle hook: prepare udev token and reconcile plugged USB drives."""
        self._ensure_udev_token()
        await self.reconcile_on_startup()

    async def close(self) -> None:
        """Moonraker lifecycle hook: cancel active sync barriers on shutdown."""
        for event in self._port_cancel_events.values():
            event.set()

    def _ensure_udev_token(self) -> str:
        if self._udev_token:
            return self._udev_token

        fallback_path = self._data_path / "comms" / "klipper_usb_load.token"
        for candidate in (self.token_path, fallback_path):
            try:
                if candidate.exists():
                    existing = candidate.read_text(encoding="utf-8").strip()
                    if existing:
                        self._udev_token = existing
                        return existing
                candidate.parent.mkdir(parents=True, exist_ok=True)
                token = secrets.token_hex(24)
                candidate.write_text(token + "\n", encoding="utf-8")
                try:
                    candidate.chmod(0o640)
                except OSError:
                    pass
                self._udev_token = token
                if candidate != self.token_path:
                    LOGGER.info("Stored udev token at fallback path %s", candidate)
                return token
            except OSError as err:
                LOGGER.debug("Unable to persist udev token at %s: %s", candidate, err)
        return ""

    async def reconcile_on_startup(self) -> None:
        """Clean stale symlinks and mount any already-connected allowed USB storage drives."""
        connected = scan_connected_storage_ports(self.cfg.sysfs_root)
        allowed_connected = [item for item in connected if self.cfg.is_port_allowed(item.port_id)]
        self.mount_manager.cleanup_stale_links({item.port_id for item in allowed_connected})

        for item in allowed_connected:
            await self.handle_drive_added(port_id=item.port_id, devnode=item.devnode)

        await self._recalculate_and_publish(notify_msg=None)

    def _get_port_lock(self, port_id: str) -> asyncio.Lock:
        lock = self._port_locks.get(port_id)
        if lock is None:
            lock = asyncio.Lock()
            self._port_locks[port_id] = lock
        return lock

    def _bump_generation(self, port_id: str) -> tuple[int, asyncio.Event]:
        prev = self._port_cancel_events.get(port_id)
        if prev is not None:
            prev.set()
        gen = self._port_generations.get(port_id, 0) + 1
        self._port_generations[port_id] = gen
        ev = asyncio.Event()
        self._port_cancel_events[port_id] = ev
        return gen, ev

    async def handle_drive_added(self, *, port_id: str, devnode: str) -> dict[str, Any]:
        """Handle a USB storage insertion or startup discovery."""
        validate_port_id(port_id)
        validate_devnode(devnode)

        if not self.cfg.is_port_allowed(port_id):
            LOGGER.info(
                "Ignored USB drive %s on port %s (not in allowed_ports=%s)",
                devnode,
                port_id,
                list(self.cfg.allowed_ports),
            )
            await self.bridge.publish_state(
                newest_file=self._newest_file,
                total_count=self._total_file_count(),
                status_text="",
                notify_msg=f"USB blocked: {port_id}",
                lcd_notifications=self.cfg.lcd_notifications,
                update_macro_state=False,
            )
            return {"status": "ignored", "reason": "port_not_allowed", "port_id": port_id}

        gen, cancel_event = self._bump_generation(port_id)
        async with self._get_port_lock(port_id):
            if self._port_generations.get(port_id) != gen or cancel_event.is_set():
                return {"status": "cancelled", "port_id": port_id}

            mount_point = await self.mount_manager.mount_drive(devnode, port_id)
            scanned_files = self.mount_manager.scan_mount_for_gcodes(
                mount_point,
                port_id,
                max_depth=self.cfg.max_scan_depth,
                extensions=self.cfg.gcode_extensions,
            )
            self.mount_manager.materialize_symlinks(port_id, scanned_files)

            drive = MountedDrive(
                port_id=port_id,
                devnode=devnode,
                gcode_subdir=self.mount_manager.port_gcode_rel_dir(port_id),
                files=scanned_files,
            )
            self._mounted_drives[port_id] = drive

            last_lcd_time = 0.0

            async def on_progress(progress: SyncProgress) -> None:
                nonlocal last_lcd_time
                now = time.monotonic()
                if (
                    progress.state == "syncing"
                    and self.cfg.lcd_notifications
                    and (now - last_lcd_time >= 1.5)
                ):
                    last_lcd_time = now
                    await self.bridge.publish_state(
                        newest_file=self._newest_file,
                        total_count=self._total_file_count(),
                        status_text=progress.message,
                        notify_msg=progress.message,
                        lcd_notifications=True,
                    )

            sync_result = await self.sync_engine.wait_for_files_loaded(
                scanned_files,
                rel_dir=drive.gcode_subdir,
                wait_for_metadata=self.cfg.wait_for_metadata,
                timeout_seconds=self.cfg.sync_timeout_seconds,
                on_progress=on_progress,
                cancel_event=cancel_event,
            )

            if sync_result.cancelled or self._port_generations.get(port_id) != gen:
                return {"status": "cancelled", "port_id": port_id}

            drive.ready_paths = set(sync_result.ready_paths)
            if sync_result.progress.warning:
                LOGGER.warning("Port %s: %s", port_id, sync_result.progress.warning)

            newest = await self._recalculate_and_publish(
                notify_msg=self._ready_notification(port_id, drive, sync_result.progress)
            )
            return {
                "status": "mounted",
                "port_id": port_id,
                "file_count": len(scanned_files),
                "newest_file": newest.rel_path if newest else None,
            }

    async def handle_drive_removed(self, *, port_id: str) -> dict[str, Any]:
        """Handle physical removal of a USB drive on port_id."""
        validate_port_id(port_id)
        self._bump_generation(port_id)

        async with self._get_port_lock(port_id):
            removed = self._mounted_drives.pop(port_id, None)
            active_filename = await self.bridge.get_active_print_filename()
            in_use = bool(
                removed is not None
                and active_filename is not None
                and active_filename.lstrip("/").startswith(f"{removed.gcode_subdir.rstrip('/')}/")
            )

            try:
                await self.mount_manager.unmount_drive(port_id, force=True)
            except MountError as err:
                LOGGER.warning("Unmount cleanup for %s raised: %s", port_id, err)

            if in_use:
                LOGGER.error(
                    "USB drive on port %s removed while printing %s!",
                    port_id,
                    active_filename,
                )
                notify_msg: Optional[str] = f"USB REMOVED MID-PRINT ({port_id})!"
            else:
                notify_msg = f"USB removed ({port_id})" if removed is not None else None

            await self._recalculate_and_publish(notify_msg=notify_msg)
            return {
                "status": "removed",
                "port_id": port_id,
                "active_print_interrupted": in_use,
            }

    async def _recalculate_and_publish(
        self,
        *,
        notify_msg: Optional[str],
    ) -> Optional[ScannedGcodeFile]:
        all_files: list[ScannedGcodeFile] = []
        available_paths: set[str] = set()
        for drive in self._mounted_drives.values():
            all_files.extend(drive.files)
            available_paths |= drive.ready_paths

        newest = select_newest_file(all_files, available_paths=available_paths)
        self._newest_file = newest

        status_text = (
            f"Ready: {newest.display_name}"
            if newest is not None
            else ("USB mounted (no G-code)" if self._mounted_drives else "No USB")
        )
        await self.bridge.publish_state(
            newest_file=newest,
            total_count=len(all_files),
            status_text=status_text,
            notify_msg=notify_msg,
            lcd_notifications=self.cfg.lcd_notifications,
        )
        return newest

    def _ready_notification(
        self,
        port_id: str,
        drive: MountedDrive,
        progress: SyncProgress,
    ) -> str:
        if progress.state == "warning":
            return f"USB {port_id}: partial ({len(drive.ready_paths)}/{len(drive.files)})"
        if self._newest_file is not None:
            return f"USB Ready: {self._newest_file.display_name}"
        return f"USB Ready ({port_id}): 0 files"

    def _total_file_count(self) -> int:
        return sum(len(drive.files) for drive in self._mounted_drives.values())

    async def _on_filelist_changed(self, *args: Any, **kwargs: Any) -> None:
        self.sync_engine.notify_filelist_changed()

    async def _on_klippy_ready(self, *args: Any, **kwargs: Any) -> None:
        self.bridge.reset_macro_cache()
        await self._recalculate_and_publish(notify_msg=None)

    async def _handle_udev_event(self, web_request: Any) -> dict[str, Any]:
        args = web_request.get_args() if hasattr(web_request, "get_args") else dict(web_request)
        expected_token = self._ensure_udev_token()
        provided_token = str(args.get("token", ""))
        if expected_token and not hmac.compare_digest(expected_token, provided_token):
            raise self._http_error("Invalid udev authentication token", status_code=403)

        action = str(args.get("action", "")).strip().lower()
        port_id = str(args.get("port_id", "")).strip()
        devnode = str(args.get("devnode", "")).strip()

        if not port_id:
            raise self._http_error("port_id is required", status_code=400)

        if action == "add":
            if not devnode:
                raise self._http_error("devnode is required for add event", status_code=400)
            try:
                return await self.handle_drive_added(port_id=port_id, devnode=devnode)
            except MountError as err:
                raise self._http_error(str(err), status_code=400) from err

        if action == "remove":
            try:
                return await self.handle_drive_removed(port_id=port_id)
            except MountError as err:
                raise self._http_error(str(err), status_code=400) from err

        raise self._http_error(f"Unsupported udev action: {action!r}", status_code=400)

    def _http_error(self, message: str, status_code: int = 400) -> Exception:
        server_error = getattr(self.server, "error", None)
        if callable(server_error):
            return server_error(message, status_code=status_code)  # type: ignore[no-any-return]
        try:
            from moonraker.utils import ServerError  # type: ignore[import-not-found]

            return ServerError(message, status_code)  # type: ignore[no-any-return]
        except ImportError:
            err = RuntimeError(message)
            setattr(err, "status_code", status_code)
            return err


def load_component(config: Any) -> KlipperUsbLoad:
    """Moonraker component entrypoint."""
    return KlipperUsbLoad(config)
