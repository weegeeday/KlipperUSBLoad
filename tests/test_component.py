"""End-to-end tests for the KlipperUsbLoad Moonraker component."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import pytest

from klipper_usb_load.component import KlipperUsbLoad, MoonrakerBridge
from klipper_usb_load.usb import UsbMountManager


class FakeWebRequest:
    def __init__(self, args: dict[str, Any]) -> None:
        self._args = args

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
                    st = path.stat()
                    self.files[rel] = {"size": st.st_size, "modified": st.st_mtime}
                    self.gcode_metadata[rel] = {
                        "slicer": "PrusaSlicer",
                        "size": st.st_size,
                        "modified": st.st_mtime,
                    }
        if self.server is not None:
            await self.server.trigger_event("file_manager:filelist_changed")

    def get_file_list(self, root: str) -> dict[str, dict[str, Any]]:
        return dict(self.files)


class FakeKlippyApis:
    def __init__(self, objects: list[str] | None = None) -> None:
        self.print_state = "standby"
        self.print_filename = ""
        self.gcodes_run: list[str] = []
        self.objects = (
            objects
            if objects is not None
            else [
                "print_stats",
                "gcode_macro _USB_LOAD_SET_STATE",
                "gcode_macro _USB_LOAD_NOTIFY",
            ]
        )

    async def get_object_list(self) -> list[str]:
        return list(self.objects)

    async def query_objects(self, objects: dict[str, list[str]]) -> dict[str, Any]:
        return {
            "print_stats": {
                "state": self.print_state,
                "filename": self.print_filename,
            }
        }

    async def run_gcode(self, script: str) -> None:
        self.gcodes_run.append(script)


class FakeServer:
    def __init__(
        self, data_path: Path, file_manager: FakeFileManager, klippy: FakeKlippyApis
    ) -> None:
        self._data_path = data_path
        self.components: dict[str, Any] = {
            "file_manager": file_manager,
            "klippy_apis": klippy,
        }
        self.endpoints: dict[str, Any] = {}
        self.event_handlers: dict[str, list[Any]] = {}

    def get_app_args(self) -> dict[str, str]:
        return {"data_path": str(self._data_path)}

    def lookup_component(self, name: str, default: Any = None) -> Any:
        return self.components.get(name, default)

    def register_endpoint(self, uri: str, methods: Any, callback: Any) -> None:
        self.endpoints[uri] = callback

    def register_event_handler(self, event: str, callback: Any) -> None:
        self.event_handlers.setdefault(event, []).append(callback)

    async def trigger_event(self, event: str) -> None:
        for cb in self.event_handlers.get(event, []):
            await cb()


class FakeMountRunner:
    def __init__(self, source_dirs_by_dev: dict[str, Path]) -> None:
        self.source_dirs_by_dev = source_dirs_by_dev

    async def mount(self, devnode: str, mount_point: Path) -> None:
        src = self.source_dirs_by_dev[devnode]
        mount_point.mkdir(parents=True, exist_ok=True)
        for item in src.iterdir():
            dst = mount_point / item.name
            if item.is_file():
                dst.write_bytes(item.read_bytes())
                st = item.stat()
                os.utime(dst, (st.st_atime, st.st_mtime))

    async def unmount(self, mount_point: Path, *, force: bool = False) -> None:
        return None


def _setup_sysfs(tmp_path: Path, ports: list[tuple[str, str]]) -> Path:
    sysfs = tmp_path / "sys"
    blk_dir = sysfs / "class" / "block"
    blk_dir.mkdir(parents=True)
    for idx, (port_id, dev_name) in enumerate(ports, start=1):
        blk = blk_dir / dev_name
        blk.mkdir()
        (blk / "device_path").write_text(
            f"/sys/devices/usb1/{port_id}/{port_id}:1.0/block/{dev_name}",
            encoding="utf-8",
        )
        usb_dev = sysfs / "bus" / "usb" / "devices" / port_id
        usb_dev.mkdir(parents=True, exist_ok=True)
        (usb_dev / "product").write_text(f"USB_STICK_{idx}", encoding="utf-8")
    return sysfs


def _make_usb_source(path: Path, files: dict[str, float]) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    for name, mtime in files.items():
        p = path / name
        p.write_text(f"; {name}\nG28\n", encoding="utf-8")
        os.utime(p, (mtime, mtime))
    return path


def _copy_link(src: str, dst: str) -> None:
    Path(dst).write_bytes(Path(src).read_bytes())
    st = Path(src).stat()
    os.utime(dst, (st.st_atime, st.st_mtime))


def test_startup_allowlist_nonblocking_udev_and_ready_notification(tmp_path: Path) -> None:
    async def scenario() -> None:
        data_path = tmp_path / "printer_data"
        gcodes_dir = data_path / "gcodes"
        gcodes_dir.mkdir(parents=True)

        usb1 = _make_usb_source(tmp_path / "u1", {"old.gcode": 100.0, "new1.gcode": 200.0})
        usb2 = _make_usb_source(tmp_path / "u2", {"new2.gcode": 500.0})
        sysfs = _setup_sysfs(tmp_path, [("1-1.2", "sda1"), ("1-1.4", "sdb1")])

        file_manager = FakeFileManager(gcodes_dir)
        klippy = FakeKlippyApis()
        server = FakeServer(data_path, file_manager, klippy)
        file_manager.server = server

        mount_mgr = UsbMountManager(
            gcodes_dir=gcodes_dir,
            mount_base=tmp_path / "run",
            runner=FakeMountRunner({"/dev/sda1": usb1, "/dev/sdb1": usb2}),
            link_fn=_copy_link,
        )

        stale = gcodes_dir / "usb" / "port_9-9.9"
        stale.mkdir(parents=True)

        comp = KlipperUsbLoad(
            {
                "server": server,
                "allowed_ports": "1-1.2",
                "sysfs_root": str(sysfs),
                "mount_base": str(tmp_path / "run"),
                "sync_timeout_seconds": 5.0,
            },
            mount_manager=mount_mgr,
        )
        await comp.component_init()
        await comp.wait_pending_tasks()

        assert list(server.endpoints.keys()) == ["/server/klipper_usb_load/udev-event"]
        assert not stale.exists()
        assert comp._newest_file is not None
        assert comp._newest_file.rel_path == "usb/USB_STICK_1/new1.gcode"
        assert any('MSG="USB Ready: new1.gcode"' in cmd for cmd in klippy.gcodes_run)

        token = comp._ensure_udev_token()
        # Disallowed port 1-1.4 is accepted asynchronously, ignored, and shows USB name + port ID
        accepted = await comp._handle_udev_event(
            FakeWebRequest(
                {
                    "action": "add",
                    "port_id": "1-1.4",
                    "devnode": "/dev/sdb1",
                    "fs_label": "BLOCKED_STICK",
                    "token": token,
                }
            )
        )
        assert accepted == {"status": "accepted", "action": "add", "port_id": "1-1.4"}
        await comp.wait_pending_tasks()
        assert any("BLOCKED_STICK blocked (1-1.4)" in cmd for cmd in klippy.gcodes_run)

        # Invalid token must be rejected
        with pytest.raises(RuntimeError):
            await comp._handle_udev_event(
                FakeWebRequest(
                    {"action": "add", "port_id": "1-1.2", "devnode": "/dev/sda1", "token": "bad"}
                )
            )

        # Removal while actively printing triggers urgent LCD warning with USB name
        klippy.print_state = "printing"
        klippy.print_filename = "usb/USB_STICK_1/new1.gcode"
        rem_accepted = await comp._handle_udev_event(
            FakeWebRequest({"action": "remove", "port_id": "1-1.2", "token": token})
        )
        assert rem_accepted["status"] == "accepted"
        await comp.wait_pending_tasks()
        assert any("USB REMOVED MID-PRINT (USB_STICK_1)!" in cmd for cmd in klippy.gcodes_run)

        await comp.close()

    asyncio.run(scenario())


def test_missing_macros_fallback_and_stale_metadata_rejection(tmp_path: Path) -> None:
    async def scenario() -> None:
        data_path = tmp_path / "printer_data"
        gcodes_dir = data_path / "gcodes"
        gcodes_dir.mkdir(parents=True)

        usb_src = _make_usb_source(tmp_path / "u", {"part.gcode": 100.0})
        sysfs = _setup_sysfs(tmp_path, [("1-1.1", "sda1")])
        file_manager = FakeFileManager(gcodes_dir)
        klippy = FakeKlippyApis(objects=["print_stats"])
        server = FakeServer(data_path, file_manager, klippy)
        file_manager.server = server

        # Verify stale metadata with wrong size is rejected by MoonrakerBridge
        file_manager.gcode_metadata["usb/port_1-1.1/part.gcode"] = {"size": 999999, "modified": 1.0}
        bridge = MoonrakerBridge(server)
        assert (
            await bridge.is_metadata_ready(
                "usb/port_1-1.1/part.gcode", expected_size=18, expected_mtime=100.0
            )
            is False
        )

        blocked_parent = tmp_path / "not_a_dir"
        blocked_parent.write_text("x", encoding="utf-8")

        mount_mgr = UsbMountManager(
            gcodes_dir=gcodes_dir,
            mount_base=tmp_path / "run",
            runner=FakeMountRunner({"/dev/sda1": usb_src}),
            link_fn=_copy_link,
        )

        comp = KlipperUsbLoad(
            {
                "server": server,
                "sysfs_root": str(sysfs),
                "mount_base": str(tmp_path / "run"),
                "udev_token_path": str(blocked_parent / "udev.token"),
            },
            mount_manager=mount_mgr,
        )
        await comp.component_init()
        await comp.wait_pending_tasks()

        fallback_token = data_path / "comms" / "klipper_usb_load.token"
        assert fallback_token.exists()
        assert not any(c.startswith("_USB_LOAD_SET_STATE") for c in klippy.gcodes_run)
        assert any(c.startswith("M117 ") for c in klippy.gcodes_run)

        klippy.gcodes_run.clear()
        klippy.objects.extend(["gcode_macro _USB_LOAD_SET_STATE", "gcode_macro _USB_LOAD_NOTIFY"])
        await server.trigger_event("server:klippy_ready")
        assert any(c.startswith("_USB_LOAD_SET_STATE") for c in klippy.gcodes_run)

        await comp.close()

    asyncio.run(scenario())
