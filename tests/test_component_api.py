"""End-to-end tests for the KlipperUsbLoad Moonraker component."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import pytest

from klipper_usb_load.component import KlipperUsbLoad
from klipper_usb_load.mount_manager import MountManager


class FakeWebRequest:
    def __init__(self, action: str = "GET", args: dict[str, Any] | None = None) -> None:
        self._action = action
        self._args = args or {}

    def get_action(self) -> str:
        return self._action

    def get_args(self) -> dict[str, Any]:
        return dict(self._args)


class FakeFileManager:
    def __init__(self, gcodes_dir: Path) -> None:
        self.gcodes_dir = gcodes_dir
        self.files: dict[str, dict[str, Any]] = {}
        self.gcode_metadata: dict[str, dict[str, Any]] = {}
        self.server: FakeServer | None = None

    async def refresh_directory(self, root: str, rel_dir: str) -> None:
        target = self.gcodes_dir / rel_dir
        if target.is_dir():
            for path in target.rglob("*"):
                if path.is_file():
                    rel = path.relative_to(self.gcodes_dir).as_posix()
                    self.files[rel] = {"size": path.stat().st_size, "modified": path.stat().st_mtime}
                    self.gcode_metadata[rel] = {"slicer": "PrusaSlicer", "size": path.stat().st_size}
        if self.server is not None:
            await self.server.trigger_event("file_manager:filelist_changed")

    def get_file_list(self, root: str) -> dict[str, dict[str, Any]]:
        return dict(self.files)


class FakeKlippyApis:
    def __init__(self) -> None:
        self.print_state = "standby"
        self.print_filename = ""
        self.gcodes_run: list[str] = []
        self.started_prints: list[str] = []

    async def query_objects(self, objects: dict[str, list[str]]) -> dict[str, Any]:
        return {
            "print_stats": {
                "state": self.print_state,
                "filename": self.print_filename,
            }
        }

    async def run_gcode(self, script: str) -> None:
        self.gcodes_run.append(script)

    async def start_print(self, filename: str) -> None:
        self.started_prints.append(filename)


class FakeServer:
    def __init__(self, data_path: Path, file_manager: FakeFileManager, klippy: FakeKlippyApis) -> None:
        self._data_path = data_path
        self.components: dict[str, Any] = {
            "file_manager": file_manager,
            "klippy_apis": klippy,
        }
        self.endpoints: dict[str, Any] = {}
        self.event_handlers: dict[str, list[Any]] = {}
        self.notifications: list[str] = []
        self.sent_events: list[tuple[str, Any]] = []

    def get_app_args(self) -> dict[str, str]:
        return {"data_path": str(self._data_path)}

    def lookup_component(self, name: str, default: Any = None) -> Any:
        return self.components.get(name, default)

    def register_endpoint(self, uri: str, methods: Any, callback: Any) -> None:
        self.endpoints[uri] = callback

    def register_event_handler(self, event: str, callback: Any) -> None:
        self.event_handlers.setdefault(event, []).append(callback)

    def register_notification(self, event: str) -> None:
        self.notifications.append(event)

    def send_event(self, event: str, payload: Any) -> None:
        self.sent_events.append((event, payload))

    async def trigger_event(self, event: str) -> None:
        for cb in self.event_handlers.get(event, []):
            await cb()


class FakeMountRunner:
    def __init__(self, source_dirs_by_dev: dict[str, Path]) -> None:
        self.source_dirs_by_dev = source_dirs_by_dev
        self.mounted: dict[str, str] = {}
        self.unmounted: list[tuple[str, bool]] = []

    async def mount(self, devnode: str, mount_point: Path) -> None:
        src = self.source_dirs_by_dev[devnode]
        mount_point.mkdir(parents=True, exist_ok=True)
        for item in src.iterdir():
            dst = mount_point / item.name
            if item.is_file():
                dst.write_bytes(item.read_bytes())
                st = item.stat()
                os.utime(dst, (st.st_atime, st.st_mtime))
        self.mounted[str(mount_point)] = devnode

    async def unmount(self, mount_point: Path, *, force: bool = False) -> None:
        self.unmounted.append((str(mount_point), force))
        self.mounted.pop(str(mount_point), None)


def _setup_fake_sysfs(tmp_path: Path, storage_ports: list[tuple[str, str]]) -> Path:
    sysfs = tmp_path / "sys"
    usb_dir = sysfs / "bus" / "usb" / "devices"
    blk_dir = sysfs / "class" / "block"
    usb_dir.mkdir(parents=True)
    blk_dir.mkdir(parents=True)

    for idx, (port_id, dev_name) in enumerate(storage_ports, start=1):
        dev = usb_dir / port_id
        dev.mkdir()
        (dev / "product").write_text(f"Drive {idx}", encoding="utf-8")
        iface = dev / "iface_1.0"
        iface.mkdir()
        (iface / "bInterfaceClass").write_text("08", encoding="utf-8")

        blk = blk_dir / dev_name
        blk.mkdir()
        (blk / "device_path").write_text(
            f"/sys/devices/usb1/{port_id}/{port_id}:1.0/block/{dev_name}",
            encoding="utf-8",
        )
    return sysfs


def _make_usb_source(path: Path, files: dict[str, float]) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    for name, mtime in files.items():
        file_path = path / name
        file_path.write_text(f"; gcode {name}\nG28\n", encoding="utf-8")
        os.utime(file_path, (mtime, mtime))
    return path


def _copy_link(src: str, dst: str) -> None:
    Path(dst).write_bytes(Path(src).read_bytes())
    st = Path(src).stat()
    os.utime(dst, (st.st_atime, st.st_mtime))


def test_component_startup_allowlist_detect_multidrive_and_print_protection(tmp_path: Path) -> None:
    async def scenario() -> None:
        data_path = tmp_path / "printer_data"
        gcodes_dir = data_path / "gcodes"
        gcodes_dir.mkdir(parents=True)

        usb1_src = _make_usb_source(
            tmp_path / "usb_src_1",
            {"older.gcode": 1000.0, "newer.gcode": 2000.0},
        )
        usb2_src = _make_usb_source(
            tmp_path / "usb_src_2",
            {"brand_new.gcode": 5000.0},
        )

        sysfs = _setup_fake_sysfs(tmp_path, [("1-1.2", "sda1"), ("1-1.4", "sdb1")])
        file_manager = FakeFileManager(gcodes_dir)
        klippy = FakeKlippyApis()
        server = FakeServer(data_path, file_manager, klippy)
        file_manager.server = server

        runner = FakeMountRunner({"/dev/sda1": usb1_src, "/dev/sdb1": usb2_src})
        mount_mgr = MountManager(
            gcodes_dir=gcodes_dir,
            mount_base=tmp_path / "run",
            runner=runner,
            link_fn=_copy_link,
        )

        # Create a stale symlink folder before startup to verify cleanup
        stale = gcodes_dir / "usb" / "port_9-9.9"
        stale.mkdir(parents=True)

        comp = KlipperUsbLoad(
            {
                "server": server,
                "sysfs_root": str(sysfs),
                "mount_base": str(tmp_path / "run"),
                "sync_timeout_seconds": 5.0,
            },
            mount_manager=mount_mgr,
            token_path=tmp_path / "run" / "udev.token",
        )
        await comp.component_init()

        assert not stale.exists()
        status = await comp.build_status_payload()
        assert status["newest_file"]["rel_path"] == "usb/port_1-1.4/brand_new.gcode"
        assert len(status["drives"]) == 2

        # Restrict allowlist to only 1-1.2 via Settings API -> 1-1.4 unmounts automatically
        updated = await comp._handle_settings(
            FakeWebRequest(
                "POST",
                {
                    "port_policy": "allowlist",
                    "allowed_ports": ["1-1.2"],
                    "port_labels": {"1-1.2": "Front Port"},
                },
            )
        )
        assert len(updated["drives"]) == 1
        assert updated["drives"][0]["port_id"] == "1-1.2"
        assert updated["newest_file"]["rel_path"] == "usb/port_1-1.2/newer.gcode"

        # Verify blocked port is ignored on udev add, but captured by Detect Port!
        await comp._handle_detect_start(FakeWebRequest("POST", {"duration_seconds": 30}))
        token = comp._ensure_udev_token()
        udev_res = await comp._handle_udev_event(
            FakeWebRequest(
                "POST",
                {
                    "action": "add",
                    "port_id": "1-1.4",
                    "devnode": "/dev/sdb1",
                    "token": token,
                },
            )
        )
        assert udev_res["status"] == "ignored"
        detect_status = await comp.build_status_payload()
        assert detect_status["detect_mode"]["detected_port_id"] == "1-1.4"

        # Invalid udev token must be rejected
        with pytest.raises(RuntimeError):
            await comp._handle_udev_event(
                FakeWebRequest(
                    "POST",
                    {"action": "add", "port_id": "1-1.2", "devnode": "/dev/sda1", "token": "bad"},
                )
            )

        # Print newest works when idle
        print_res = await comp._handle_print_newest(FakeWebRequest("POST", {}))
        assert print_res["status"] == "started"
        assert klippy.started_prints == ["usb/port_1-1.2/newer.gcode"]

        # Simulate active print using 1-1.2 -> UI unmount is blocked with 409
        klippy.print_state = "printing"
        klippy.print_filename = "usb/port_1-1.2/newer.gcode"
        with pytest.raises(RuntimeError) as exc_info:
            await comp._handle_unmount(FakeWebRequest("POST", {"port_id": "1-1.2"}))
        assert getattr(exc_info.value, "status_code", None) == 409

        # Physical removal mid-print triggers explicit warning
        rem_res = await comp._handle_udev_event(
            FakeWebRequest(
                "POST",
                {"action": "remove", "port_id": "1-1.2", "token": token},
            )
        )
        assert rem_res["active_print_interrupted"] is True
        after_rem = await comp.build_status_payload()
        assert "removed while printing" in after_rem["warning"].lower()

        await comp.close()

    asyncio.run(scenario())


def test_missing_klipper_macros_skips_macro_calls_and_recovers_on_klippy_ready(tmp_path: Path) -> None:
    async def scenario() -> None:
        data_path = tmp_path / "printer_data"
        gcodes_dir = data_path / "gcodes"
        gcodes_dir.mkdir(parents=True)

        usb_src = _make_usb_source(tmp_path / "usb_src", {"part.gcode": 1200.0})
        sysfs = _setup_fake_sysfs(tmp_path, [("1-1.2", "sda1")])
        file_manager = FakeFileManager(gcodes_dir)

        class MacroAwareKlippy(FakeKlippyApis):
            def __init__(self) -> None:
                super().__init__()
                self.objects = ["print_stats", "idle_timeout"]

            async def get_object_list(self) -> list[str]:
                return list(self.objects)

        klippy = MacroAwareKlippy()
        server = FakeServer(data_path, file_manager, klippy)
        file_manager.server = server

        runner = FakeMountRunner({"/dev/sda1": usb_src})
        mount_mgr = MountManager(
            gcodes_dir=gcodes_dir,
            mount_base=tmp_path / "run",
            runner=runner,
            link_fn=_copy_link,
        )

        # Point token_path at a non-writable/file-blocked location to verify fallback token path
        blocked_parent = tmp_path / "blocked_file"
        blocked_parent.write_text("not a dir", encoding="utf-8")

        comp = KlipperUsbLoad(
            {
                "server": server,
                "sysfs_root": str(sysfs),
                "mount_base": str(tmp_path / "run"),
                "sync_timeout_seconds": 5.0,
            },
            mount_manager=mount_mgr,
            token_path=blocked_parent / "udev.token",
        )
        await comp.component_init()

        fallback_token = data_path / "comms" / "klipper_usb_load.token"
        assert fallback_token.exists()
        assert comp._ensure_udev_token() == fallback_token.read_text(encoding="utf-8").strip()

        status = await comp.build_status_payload()
        assert status["klipper_macros_loaded"] is False
        assert not any(cmd.startswith("_USB_LOAD_SET_STATE") for cmd in klippy.gcodes_run)
        assert not any(cmd.startswith("_USB_LOAD_NOTIFY") for cmd in klippy.gcodes_run)
        assert any(cmd.startswith("M117 ") for cmd in klippy.gcodes_run)

        # Now user adds [include klipper_usb_load.cfg] and Klippy becomes ready
        klippy.gcodes_run.clear()
        klippy.objects.extend(["gcode_macro _USB_LOAD_SET_STATE", "gcode_macro _USB_LOAD_NOTIFY"])
        await server.trigger_event("server:klippy_ready")

        status_after = await comp.build_status_payload()
        assert status_after["klipper_macros_loaded"] is True
        assert any(cmd.startswith("_USB_LOAD_SET_STATE") for cmd in klippy.gcodes_run)

        await comp.close()

    asyncio.run(scenario())
