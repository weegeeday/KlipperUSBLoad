"""Async tests for GcodeSyncEngine race-condition elimination and readiness barrier."""

from __future__ import annotations

import asyncio
from typing import Any

from klipper_usb_load.models import ScannedGcodeFile, SyncProgress
from klipper_usb_load.sync_engine import GcodeSyncEngine, select_newest_file


class SimulatedMoonrakerBackend:
    """In-memory Moonraker backend that reveals files and metadata incrementally."""

    def __init__(self) -> None:
        self.listed: dict[str, dict[str, Any]] = {}
        self.metadata_ready: set[str] = set()
        self.refresh_calls: list[str] = []
        self.parse_requests: list[str] = []
        self.list_calls: int = 0

    async def refresh_directory(self, rel_dir: str) -> None:
        self.refresh_calls.append(rel_dir)

    async def list_gcode_files(self) -> dict[str, dict[str, Any]]:
        self.list_calls += 1
        return dict(self.listed)

    async def is_metadata_ready(self, rel_path: str) -> bool:
        return rel_path in self.metadata_ready

    async def request_metadata_parse(self, rel_path: str) -> None:
        self.parse_requests.append(rel_path)


def _make_file(name: str, mtime: float) -> ScannedGcodeFile:
    return ScannedGcodeFile(
        rel_path=f"usb/port_1-1.2/{name}",
        usb_abs_path=f"/run/klipper-usb-load/1-1.2/{name}",
        display_name=name,
        drive_port_id="1-1.2",
        mtime=mtime,
        size=1024,
    )


def test_wait_barrier_waits_for_slowest_newest_file_and_metadata() -> None:
    async def scenario() -> None:
        backend = SimulatedMoonrakerBackend()
        engine = GcodeSyncEngine(backend)

        oldest = _make_file("01_oldest.gcode", 100.0)
        middle = _make_file("02_middle.gcode", 200.0)
        newest = _make_file("03_newest.gcode", 300.0)
        expected = [oldest, middle, newest]

        # Oldest file is already indexed before barrier starts (the classic race condition!)
        backend.listed[oldest.rel_path] = {"modified": 100.0, "size": 1024}
        backend.metadata_ready.add(oldest.rel_path)

        progress_events: list[SyncProgress] = []

        async def record_progress(progress: SyncProgress) -> None:
            progress_events.append(progress)

        async def simulate_slow_moonraker() -> None:
            await asyncio.sleep(0.05)
            backend.listed[middle.rel_path] = {"modified": 200.0, "size": 1024}
            engine.notify_filelist_changed()

            await asyncio.sleep(0.05)
            backend.metadata_ready.add(middle.rel_path)
            # Newest file finally enters file list, but metadata is not parsed yet
            backend.listed[newest.rel_path] = {"modified": 300.0, "size": 1024}
            engine.notify_filelist_changed()

            await asyncio.sleep(0.05)
            # Metadata finishes last -> barrier should now unblock!
            backend.metadata_ready.add(newest.rel_path)
            engine.notify_filelist_changed()

        driver = asyncio.create_task(simulate_slow_moonraker())
        result = await engine.wait_for_files_loaded(
            expected,
            rel_dir="usb/port_1-1.2",
            wait_for_metadata=True,
            timeout_seconds=2.0,
            poll_interval=0.05,
            on_progress=record_progress,
        )
        await driver

        assert not result.cancelled
        assert not result.timed_out
        assert result.progress.state == "ready"
        assert result.ready_paths == {f.rel_path for f in expected}
        assert backend.refresh_calls == ["usb/port_1-1.2"]
        assert any(p.state == "syncing" for p in progress_events)
        assert progress_events[-1].state == "ready"

        chosen = select_newest_file(expected, available_paths=result.ready_paths)
        assert chosen is not None
        assert chosen.rel_path == newest.rel_path

    asyncio.run(scenario())


def test_file_list_only_mode_does_not_wait_for_metadata() -> None:
    async def scenario() -> None:
        backend = SimulatedMoonrakerBackend()
        engine = GcodeSyncEngine(backend)

        f1 = _make_file("a.gcode", 10.0)
        f2 = _make_file("b.gcode", 20.0)
        backend.listed[f1.rel_path] = {"size": 100}
        backend.listed[f2.rel_path] = {"size": 200}

        result = await engine.wait_for_files_loaded(
            [f1, f2],
            rel_dir="usb/port_1-1.2",
            wait_for_metadata=False,
            timeout_seconds=1.0,
            poll_interval=0.05,
        )
        assert result.progress.state == "ready"
        assert result.ready_paths == {f1.rel_path, f2.rel_path}
        assert backend.parse_requests == []

    asyncio.run(scenario())


def test_timeout_falls_back_safely_and_never_picks_unindexed_file() -> None:
    async def scenario() -> None:
        backend = SimulatedMoonrakerBackend()
        engine = GcodeSyncEngine(backend)

        seen_old = _make_file("seen_old.gcode", 100.0)
        never_indexed_new = _make_file("unseen_new.gcode", 500.0)

        backend.listed[seen_old.rel_path] = {"size": 100}
        backend.metadata_ready.add(seen_old.rel_path)

        result = await engine.wait_for_files_loaded(
            [seen_old, never_indexed_new],
            rel_dir="usb/port_1-1.2",
            wait_for_metadata=True,
            timeout_seconds=0.15,
            poll_interval=0.03,
        )
        assert result.timed_out is True
        assert result.progress.state == "warning"
        assert result.progress.missing_files == [never_indexed_new.rel_path]
        chosen = select_newest_file([seen_old, never_indexed_new], available_paths=result.ready_paths)
        assert chosen is not None
        assert chosen.rel_path == seen_old.rel_path

    asyncio.run(scenario())


def test_cancel_event_stops_sync_immediately_on_unplug() -> None:
    async def scenario() -> None:
        backend = SimulatedMoonrakerBackend()
        engine = GcodeSyncEngine(backend)
        cancel_event = asyncio.Event()

        async def unplug_soon() -> None:
            await asyncio.sleep(0.04)
            cancel_event.set()

        task = asyncio.create_task(unplug_soon())
        result = await engine.wait_for_files_loaded(
            [_make_file("part.gcode", 100.0)],
            rel_dir="usb/port_1-1.2",
            wait_for_metadata=True,
            timeout_seconds=5.0,
            poll_interval=0.5,
            cancel_event=cancel_event,
        )
        await task

        assert result.cancelled is True
        assert result.progress.state == "idle"

    asyncio.run(scenario())


def test_empty_drive_completes_immediately() -> None:
    async def scenario() -> None:
        backend = SimulatedMoonrakerBackend()
        engine = GcodeSyncEngine(backend)
        result = await engine.wait_for_files_loaded(
            [],
            rel_dir="usb/port_1-1.2",
            wait_for_metadata=True,
            timeout_seconds=1.0,
        )
        assert result.progress.state == "ready"
        assert result.progress.total_files == 0

    asyncio.run(scenario())


def test_list_gcode_files_not_polled_after_all_files_seen() -> None:
    async def scenario() -> None:
        backend = SimulatedMoonrakerBackend()
        engine = GcodeSyncEngine(backend)

        f1 = _make_file("a.gcode", 10.0)
        f2 = _make_file("b.gcode", 20.0)
        # All files already in file list on tick 1, but metadata takes several ticks
        backend.listed[f1.rel_path] = {"size": 100}
        backend.listed[f2.rel_path] = {"size": 200}

        async def finish_metadata_later() -> None:
            await asyncio.sleep(0.12)
            backend.metadata_ready.add(f1.rel_path)
            backend.metadata_ready.add(f2.rel_path)
            engine.notify_filelist_changed()

        driver = asyncio.create_task(finish_metadata_later())
        result = await engine.wait_for_files_loaded(
            [f1, f2],
            rel_dir="usb/port_1-1.2",
            wait_for_metadata=True,
            timeout_seconds=2.0,
            poll_interval=0.03,
        )
        await driver

        assert result.progress.state == "ready"
        assert backend.list_calls == 1

    asyncio.run(scenario())
