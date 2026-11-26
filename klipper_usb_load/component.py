"""Main Moonraker component for KlipperUSBLoad."""

from __future__ import annotations

import asyncio
import hmac
import logging
import secrets
import time
from pathlib import Path
from typing import Any, Optional

from .config_store import SettingsStore, SettingsValidationError
from .models import (
    ActivePrintInfo,
    DetectModeState,
    MountedDriveState,
    ScannedGcodeFile,
    SyncProgress,
)
from .moonraker_adapter import MoonrakerAdapter
from .mount_manager import MountError, MountManager, validate_devnode, validate_port_id
from .sync_engine import GcodeSyncEngine, select_newest_file
from .usb_topology import UsbTopologyScanner

LOGGER = logging.getLogger("moonraker.klipper_usb_load")

WEB_DIR = Path(__file__).resolve().parent.parent / "web"


def _resolve_request_types(methods: list[str]) -> Any:
    """Resolve Moonraker RequestType enum if running inside Moonraker, else list[str]."""
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


def _read_config_value(config: Any, key: str, default: Any = None) -> Any:
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


def _read_config_float(config: Any, key: str, default: float) -> float:
    if config is None:
        return default
    if isinstance(config, dict):
        return float(config.get(key, default))
    getter = getattr(config, "getfloat", None)
    if callable(getter):
        return float(getter(key, default))
    return float(_read_config_value(config, key, default))


def _read_config_int(config: Any, key: str, default: int) -> int:
    if config is None:
        return default
    if isinstance(config, dict):
        return int(config.get(key, default))
    getter = getattr(config, "getint", None)
    if callable(getter):
        return int(getter(key, default))
    return int(_read_config_value(config, key, default))


def _read_config_bool(config: Any, key: str, default: bool) -> bool:
    if config is None:
        return default
    if isinstance(config, dict):
        return bool(config.get(key, default))
    getter = getattr(config, "getboolean", None)
    if callable(getter):
        return bool(getter(key, default))
    return bool(_read_config_value(config, key, default))


class KlipperUsbLoad:
    """Moonraker plugin orchestrating USB port filtering, mounting, and G-code sync."""

    def __init__(
        self,
        config: Any,
        *,
        settings_store: Optional[SettingsStore] = None,
        topology_scanner: Optional[UsbTopologyScanner] = None,
        mount_manager: Optional[MountManager] = None,
        adapter: Optional[MoonrakerAdapter] = None,
        token_path: Optional[Path] = None,
    ) -> None:
        self.server = config.get_server() if hasattr(config, "get_server") else config["server"]
        data_path = self._resolve_data_path()
        self._data_path = data_path

        default_settings_path = data_path / "config" / "klipper_usb_load.json"
        settings_path_str = _read_config_value(config, "settings_path", None)
        settings_path = Path(settings_path_str) if settings_path_str else default_settings_path

        defaults = {
            "max_scan_depth": _read_config_int(config, "max_scan_depth", 2),
            "wait_for_metadata": _read_config_bool(config, "wait_for_metadata", True),
            "sync_timeout_seconds": _read_config_float(config, "sync_timeout_seconds", 60.0),
            "lcd_notifications": _read_config_bool(config, "lcd_notifications", True),
            "usb_subdir_name": str(_read_config_value(config, "usb_subdir_name", "usb")),
        }

        self.settings_store = settings_store or SettingsStore(settings_path, defaults=defaults)
        self.settings_store.load()

        sysfs_root_str = _read_config_value(config, "sysfs_root", "/sys")
        self.topology = topology_scanner or UsbTopologyScanner(Path(str(sysfs_root_str)))

        gcodes_dir_str = _read_config_value(config, "gcodes_path", None)
        gcodes_dir = Path(str(gcodes_dir_str)) if gcodes_dir_str else (data_path / "gcodes")
        mount_base_str = _read_config_value(config, "mount_base", "/run/klipper-usb-load")

        self.mount_manager = mount_manager or MountManager(
            gcodes_dir=gcodes_dir,
            mount_base=Path(str(mount_base_str)),
            usb_subdir_name=self.settings_store.settings.usb_subdir_name,
        )
        self.adapter = adapter or MoonrakerAdapter(self.server)
        self.sync_engine = GcodeSyncEngine(self.adapter)

        token_cfg = _read_config_value(config, "udev_token_path", None)
        self.token_path = (
            token_path
            if token_path is not None
            else (
                Path(str(token_cfg))
                if token_cfg
                else (self.mount_manager.mount_base / "udev.token")
            )
        )
        self._udev_token: str = ""

        self._mounted_drives: dict[str, MountedDriveState] = {}
        self._ready_paths_by_port: dict[str, set[str]] = {}
        self._port_locks: dict[str, asyncio.Lock] = {}
        self._port_cancel_events: dict[str, asyncio.Event] = {}
        self._port_generations: dict[str, int] = {}
        self._newest_file: Optional[ScannedGcodeFile] = None
        self._last_warning: str = ""
        self._detect_mode = DetectModeState()
        self._detect_expiry_task: Optional[asyncio.Task[None]] = None

        self._register_endpoints()
        self._register_events()
        self._register_static_ui()

    def _resolve_data_path(self) -> Path:
        get_app_args = getattr(self.server, "get_app_args", None)
        if callable(get_app_args):
            args = get_app_args()
            if isinstance(args, dict) and args.get("data_path"):
                return Path(str(args["data_path"]))
        return Path.home() / "printer_data"

    def _register_endpoints(self) -> None:
        register_endpoint = getattr(self.server, "register_endpoint", None)
        if not callable(register_endpoint):
            return

        register_endpoint(
            "/server/klipper_usb_load/status",
            _resolve_request_types(["GET"]),
            self._handle_get_status,
        )
        register_endpoint(
            "/server/klipper_usb_load/settings",
            _resolve_request_types(["GET", "POST"]),
            self._handle_settings,
        )
        register_endpoint(
            "/server/klipper_usb_load/detect-port/start",
            _resolve_request_types(["POST"]),
            self._handle_detect_start,
        )
        register_endpoint(
            "/server/klipper_usb_load/detect-port/stop",
            _resolve_request_types(["POST"]),
            self._handle_detect_stop,
        )
        register_endpoint(
            "/server/klipper_usb_load/rescan",
            _resolve_request_types(["POST"]),
            self._handle_rescan,
        )
        register_endpoint(
            "/server/klipper_usb_load/unmount",
            _resolve_request_types(["POST"]),
            self._handle_unmount,
        )
        register_endpoint(
            "/server/klipper_usb_load/print-newest",
            _resolve_request_types(["POST"]),
            self._handle_print_newest,
        )
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

        register_notif = getattr(self.server, "register_notification", None)
        if callable(register_notif):
            register_notif("klipper_usb_load:status_changed")

    def _register_static_ui(self) -> None:
        if not WEB_DIR.is_dir():
            return
        lookup = getattr(self.server, "lookup_component", None)
        if not callable(lookup):
            return
        try:
            application = lookup("application", None)
        except TypeError:
            try:
                application = lookup("application")
            except Exception:
                return
        except Exception:
            return
        if application is None:
            return

        app = getattr(application, "app", None)
        add_handlers = getattr(app, "add_handlers", None)
        if not callable(add_handlers):
            return

        try:
            import tornado.web  # type: ignore[import-not-found]
        except ImportError:
            return

        class _RedirectUiHandler(tornado.web.RequestHandler):
            def get(self) -> None:
                self.redirect("/server/klipper_usb_load/ui/", permanent=False)

        add_handlers(
            r".*$",
            [
                (r"/server/klipper_usb_load/ui", _RedirectUiHandler),
                (
                    r"/server/klipper_usb_load/ui/(.*)",
                    tornado.web.StaticFileHandler,
                    {"path": str(WEB_DIR), "default_filename": "index.html"},
                ),
            ],
        )

    async def component_init(self) -> None:
        """Moonraker lifecycle hook: prepare auth token and reconcile attached USB drives."""
        self._ensure_udev_token()
        await self.reconcile_on_startup()

    async def close(self) -> None:
        """Moonraker lifecycle hook: cancel background tasks cleanly."""
        if self._detect_expiry_task is not None:
            self._detect_expiry_task.cancel()
            try:
                await self._detect_expiry_task
            except asyncio.CancelledError:
                pass
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
        ports = self.topology.scan_ports(
            settings=self.settings_store.settings,
            mounted_drives=self._mounted_drives,
        )
        connected_allowed_ports = {
            port.port_id
            for port in ports
            if port.connected
            and port.device_class == "storage"
            and port.devnode
            and port.allowed
        }
        self.mount_manager.cleanup_stale_links(connected_allowed_ports)

        for port in ports:
            if port.port_id in connected_allowed_ports:
                await self.handle_drive_added(
                    port_id=port.port_id,
                    devnode=port.devnode,
                    fs_label=port.fs_label,
                    fs_type=port.fs_type,
                )

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

    async def handle_drive_added(
        self,
        *,
        port_id: str,
        devnode: str,
        fs_label: str = "",
        fs_type: str = "",
    ) -> dict[str, Any]:
        """Process a newly attached USB storage device."""
        validate_port_id(port_id)
        validate_devnode(devnode)

        if self._detect_mode.active and time.time() <= self._detect_mode.expires_at:
            self._detect_mode.detected_port_id = port_id
            self._detect_mode.detected_device = {
                "port_id": port_id,
                "devnode": devnode,
                "fs_label": fs_label,
                "fs_type": fs_type,
            }
            self._emit_status_changed()
        elif self._detect_mode.active:
            self._detect_mode.active = False

        if not self.settings_store.is_port_allowed(port_id):
            LOGGER.info("Ignoring USB drive %s on disallowed port %s", devnode, port_id)
            self._emit_status_changed()
            return {"status": "ignored", "reason": "port_not_allowed", "port_id": port_id}

        gen, cancel_event = self._bump_generation(port_id)
        async with self._get_port_lock(port_id):
            if self._port_generations.get(port_id) != gen or cancel_event.is_set():
                return {"status": "cancelled", "port_id": port_id}

            settings = self.settings_store.settings
            mount_point = await self.mount_manager.mount_drive(devnode, port_id)

            scanning_progress = SyncProgress(
                state="scanning",
                message=f"Scanning USB ({port_id})...",
            )
            drive_state = MountedDriveState(
                port_id=port_id,
                devnode=devnode,
                mount_point=str(mount_point),
                gcode_subdir=self.mount_manager.port_gcode_rel_dir(port_id),
                fs_label=fs_label,
                fs_type=fs_type,
                files=[],
                sync_progress=scanning_progress,
                mounted_at=time.time(),
            )
            self._mounted_drives[port_id] = drive_state
            self._emit_status_changed()

            scanned_files = self.mount_manager.scan_mount_for_gcodes(
                mount_point,
                port_id,
                max_depth=settings.max_scan_depth,
                extensions=settings.gcode_extensions,
            )
            self.mount_manager.materialize_symlinks(port_id, scanned_files)

            drive_state.files = scanned_files
            drive_state.sync_progress = SyncProgress(
                state="syncing" if scanned_files else "ready",
                total_files=len(scanned_files),
                seen_files=0,
                metadata_ready_files=0,
                missing_files=[f.rel_path for f in scanned_files],
                message=(
                    f"USB: Loading 0/{len(scanned_files)}"
                    if scanned_files
                    else "USB mounted (no G-code files found)"
                ),
            )
            self._emit_status_changed()

            last_lcd_time = 0.0

            async def on_progress(progress: SyncProgress) -> None:
                nonlocal last_lcd_time
                drive_state.sync_progress = progress
                self._emit_status_changed()
                now = time.monotonic()
                if (
                    progress.state == "syncing"
                    and settings.lcd_notifications
                    and (now - last_lcd_time >= 1.5)
                ):
                    last_lcd_time = now
                    await self.adapter.publish_usb_state(
                        newest_file=self._newest_file,
                        total_count=self._total_file_count(),
                        status_text=progress.message,
                        notify_msg=progress.message,
                        lcd_notifications=True,
                    )

            sync_result = await self.sync_engine.wait_for_files_loaded(
                scanned_files,
                rel_dir=drive_state.gcode_subdir,
                wait_for_metadata=settings.wait_for_metadata,
                timeout_seconds=settings.sync_timeout_seconds,
                on_progress=on_progress,
                cancel_event=cancel_event,
            )

            if sync_result.cancelled or self._port_generations.get(port_id) != gen:
                return {"status": "cancelled", "port_id": port_id}

            drive_state.sync_progress = sync_result.progress
            self._ready_paths_by_port[port_id] = set(sync_result.ready_paths)
            if sync_result.progress.warning:
                self._last_warning = sync_result.progress.warning
            else:
                self._last_warning = ""

            newest = await self._recalculate_and_publish(
                notify_msg=self._build_ready_notification(port_id, drive_state)
            )
            return {
                "status": "mounted",
                "port_id": port_id,
                "file_count": len(scanned_files),
                "newest_file": newest.to_dict() if newest else None,
                "sync_progress": sync_result.progress.to_dict(),
            }

    async def handle_drive_removed(self, *, port_id: str, force: bool = True) -> dict[str, Any]:
        """Process physical or manual removal of a USB drive on port_id."""
        validate_port_id(port_id)
        self._bump_generation(port_id)

        async with self._get_port_lock(port_id):
            removed_drive = self._mounted_drives.pop(port_id, None)
            self._ready_paths_by_port.pop(port_id, None)

            active_print = await self.adapter.get_active_print_info()
            in_use = (
                removed_drive is not None
                and active_print.is_active
                and self._is_print_using_drive(active_print, removed_drive)
            )

            try:
                await self.mount_manager.unmount_drive(port_id, force=force)
            except MountError as err:
                LOGGER.warning("Unmount cleanup for %s raised: %s", port_id, err)

            if in_use:
                warning = (
                    f"WARNING: USB drive on port {port_id} was removed while printing "
                    f"{active_print.filename}!"
                )
                self._last_warning = warning
                notify_msg: Optional[str] = f"USB REMOVED MID-PRINT ({port_id})!"
            else:
                notify_msg = f"USB removed ({port_id})" if removed_drive is not None else None

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
        for pid, drive in self._mounted_drives.items():
            all_files.extend(drive.files)
            ready = self._ready_paths_by_port.get(pid)
            if ready is not None:
                available_paths |= ready

        newest = select_newest_file(all_files, available_paths=available_paths)
        self._newest_file = newest

        status_text = (
            f"Ready: {newest.display_name}"
            if newest is not None
            else ("USB mounted (no G-code)" if self._mounted_drives else "No USB")
        )
        await self.adapter.publish_usb_state(
            newest_file=newest,
            total_count=len(all_files),
            status_text=status_text,
            notify_msg=notify_msg,
            lcd_notifications=self.settings_store.settings.lcd_notifications,
        )
        self._emit_status_changed()
        return newest

    def _build_ready_notification(
        self,
        port_id: str,
        drive_state: MountedDriveState,
    ) -> str:
        if drive_state.sync_progress.state == "warning":
            return f"USB {port_id}: partial load ({len(drive_state.files)} files)"
        if self._newest_file is not None:
            return f"USB Ready: {self._newest_file.display_name}"
        return f"USB Ready ({port_id}): 0 files"

    def _total_file_count(self) -> int:
        return sum(len(d.files) for d in self._mounted_drives.values())

    def _is_print_using_drive(
        self,
        active_print: ActivePrintInfo,
        drive: MountedDriveState,
    ) -> bool:
        if not active_print.filename:
            return False
        normalized = active_print.filename.lstrip("/")
        prefix = f"{drive.gcode_subdir.rstrip('/')}/"
        return normalized.startswith(prefix)

    async def build_status_payload(self) -> dict[str, Any]:
        """Build the complete JSON status payload for REST and WebSocket consumers."""
        settings = self.settings_store.settings
        ports = self.topology.scan_ports(
            settings=settings,
            mounted_drives=self._mounted_drives,
        )
        active_print = await self.adapter.get_active_print_info()

        if self._detect_mode.active and time.time() > self._detect_mode.expires_at:
            self._detect_mode.active = False

        drives_payload: list[dict[str, Any]] = []
        for drive in self._mounted_drives.values():
            entry = drive.to_dict()
            entry["in_use_by_print"] = (
                active_print.is_active and self._is_print_using_drive(active_print, drive)
            )
            drives_payload.append(entry)

        return {
            "settings": settings.to_dict(),
            "ports": [p.to_dict() for p in ports],
            "drives": drives_payload,
            "newest_file": self._newest_file.to_dict() if self._newest_file else None,
            "active_print": active_print.to_dict(),
            "detect_mode": self._detect_mode.to_dict(),
            "warning": self._last_warning,
            "klipper_macros_loaded": self.adapter.macros_loaded,
        }

    def _emit_status_changed(self) -> None:
        send_notif = getattr(self.server, "send_event", None)
        if callable(send_notif):
            try:
                send_notif(
                    "klipper_usb_load:status_changed",
                    {
                        "newest_file": self._newest_file.to_dict() if self._newest_file else None,
                        "drives": [d.to_dict() for d in self._mounted_drives.values()],
                        "detect_mode": self._detect_mode.to_dict(),
                        "warning": self._last_warning,
                    },
                )
            except Exception:
                pass

    async def _on_filelist_changed(self, *args: Any, **kwargs: Any) -> None:
        self.sync_engine.notify_filelist_changed()

    async def _on_klippy_ready(self, *args: Any, **kwargs: Any) -> None:
        self.adapter.reset_macro_cache()
        await self._recalculate_and_publish(notify_msg=None)

    async def _handle_get_status(self, web_request: Any) -> dict[str, Any]:
        return await self.build_status_payload()

    async def _handle_settings(self, web_request: Any) -> dict[str, Any]:
        action = str(web_request.get_action()).upper() if hasattr(web_request, "get_action") else "GET"
        if action == "GET":
            return {"settings": self.settings_store.settings.to_dict()}

        payload = self._extract_args(web_request)
        try:
            updated = self.settings_store.update(payload)
        except SettingsValidationError as err:
            raise self._http_error(str(err), status_code=400) from err

        self.mount_manager.set_usb_subdir_name(updated.usb_subdir_name)
        await self._reconcile_settings_change()
        return await self.build_status_payload()

    async def _reconcile_settings_change(self) -> None:
        active_print = await self.adapter.get_active_print_info()
        settings = self.settings_store.settings

        for pid in list(self._mounted_drives.keys()):
            if not settings.is_port_allowed(pid):
                drive = self._mounted_drives[pid]
                if active_print.is_active and self._is_print_using_drive(active_print, drive):
                    self._last_warning = (
                        f"Port {pid} removed from allowlist, but left mounted until active "
                        "print finishes."
                    )
                else:
                    await self.handle_drive_removed(port_id=pid, force=False)

        ports = self.topology.scan_ports(settings=settings, mounted_drives=self._mounted_drives)
        for port in ports:
            if (
                port.connected
                and port.device_class == "storage"
                and port.devnode
                and port.allowed
                and port.port_id not in self._mounted_drives
            ):
                await self.handle_drive_added(
                    port_id=port.port_id,
                    devnode=port.devnode,
                    fs_label=port.fs_label,
                    fs_type=port.fs_type,
                )

        self._emit_status_changed()

    async def _handle_detect_start(self, web_request: Any) -> dict[str, Any]:
        args = self._extract_args(web_request)
        duration = float(args.get("duration_seconds", 30.0))
        duration = max(5.0, min(120.0, duration))

        self._detect_mode = DetectModeState(
            active=True,
            expires_at=time.time() + duration,
            detected_port_id=None,
            detected_device=None,
        )
        if self._detect_expiry_task is not None:
            self._detect_expiry_task.cancel()
        self._detect_expiry_task = asyncio.create_task(self._expire_detect_mode(duration))
        self._emit_status_changed()
        return {"detect_mode": self._detect_mode.to_dict()}

    async def _expire_detect_mode(self, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
            if self._detect_mode.active and time.time() >= self._detect_mode.expires_at:
                self._detect_mode.active = False
                self._emit_status_changed()
        except asyncio.CancelledError:
            return

    async def _handle_detect_stop(self, web_request: Any) -> dict[str, Any]:
        if self._detect_expiry_task is not None:
            self._detect_expiry_task.cancel()
        self._detect_mode.active = False
        self._emit_status_changed()
        return {"detect_mode": self._detect_mode.to_dict()}

    async def _handle_rescan(self, web_request: Any) -> dict[str, Any]:
        args = self._extract_args(web_request)
        target_port = args.get("port_id")
        ports = self.topology.scan_ports(
            settings=self.settings_store.settings,
            mounted_drives=self._mounted_drives,
        )
        for port in ports:
            if target_port and port.port_id != target_port:
                continue
            if port.connected and port.device_class == "storage" and port.devnode and port.allowed:
                await self.handle_drive_added(
                    port_id=port.port_id,
                    devnode=port.devnode,
                    fs_label=port.fs_label,
                    fs_type=port.fs_type,
                )
        return await self.build_status_payload()

    async def _handle_unmount(self, web_request: Any) -> dict[str, Any]:
        args = self._extract_args(web_request)
        port_id = str(args.get("port_id", "")).strip()
        if not port_id:
            raise self._http_error("port_id is required", status_code=400)

        drive = self._mounted_drives.get(port_id)
        if drive is None:
            raise self._http_error(f"No mounted USB drive on port {port_id}", status_code=404)

        active_print = await self.adapter.get_active_print_info()
        if active_print.is_active and self._is_print_using_drive(active_print, drive):
            raise self._http_error(
                f"Cannot unmount port {port_id} while actively printing {active_print.filename}",
                status_code=409,
            )

        await self.handle_drive_removed(port_id=port_id, force=False)
        return await self.build_status_payload()

    async def _handle_print_newest(self, web_request: Any) -> dict[str, Any]:
        active_print = await self.adapter.get_active_print_info()
        if active_print.is_active:
            raise self._http_error(
                f"Printer is busy ({active_print.state}): {active_print.filename}",
                status_code=409,
            )

        if self._newest_file is None:
            raise self._http_error("No ready USB G-code file is available", status_code=409)

        drive = self._mounted_drives.get(self._newest_file.drive_port_id)
        if drive is not None and drive.sync_progress.state == "syncing":
            raise self._http_error("USB files are still indexing in Moonraker", status_code=409)

        await self.adapter.start_print(self._newest_file.rel_path)
        return {"status": "started", "file": self._newest_file.to_dict()}

    async def _handle_udev_event(self, web_request: Any) -> dict[str, Any]:
        args = self._extract_args(web_request)
        expected_token = self._ensure_udev_token()
        provided_token = str(args.get("token", ""))
        if expected_token and not hmac.compare_digest(expected_token, provided_token):
            raise self._http_error("Invalid udev authentication token", status_code=403)

        action = str(args.get("action", "")).strip().lower()
        port_id = str(args.get("port_id", "")).strip()
        devnode = str(args.get("devnode", "")).strip()
        fs_label = str(args.get("fs_label", "")).strip()
        fs_type = str(args.get("fs_type", "")).strip()

        if not port_id:
            raise self._http_error("port_id is required", status_code=400)

        if action == "add":
            if not devnode:
                raise self._http_error("devnode is required for add event", status_code=400)
            try:
                return await self.handle_drive_added(
                    port_id=port_id,
                    devnode=devnode,
                    fs_label=fs_label,
                    fs_type=fs_type,
                )
            except MountError as err:
                raise self._http_error(str(err), status_code=400) from err

        if action == "remove":
            try:
                return await self.handle_drive_removed(port_id=port_id, force=True)
            except MountError as err:
                raise self._http_error(str(err), status_code=400) from err

        raise self._http_error(f"Unsupported udev action: {action!r}", status_code=400)

    def _extract_args(self, web_request: Any) -> dict[str, Any]:
        if hasattr(web_request, "get_args"):
            raw = web_request.get_args()
            if isinstance(raw, dict):
                return raw
        if isinstance(web_request, dict):
            return web_request
        return {}

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
