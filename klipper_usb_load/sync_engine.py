"""Two-phase G-code synchronization barrier and newest-file selector."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional, Protocol, Sequence

from .models import ScannedGcodeFile, SyncProgress


class MoonrakerFileBackend(Protocol):
    """Abstract interface over Moonraker's file_manager for testability."""

    async def refresh_directory(self, rel_dir: str) -> None:
        """Ask Moonraker to scan/refresh a relative directory inside gcodes/."""

    async def list_gcode_files(self) -> dict[str, dict[str, Any]]:
        """Return a mapping of POSIX rel_path -> file entry dict."""

    async def is_metadata_ready(self, rel_path: str) -> bool:
        """Return True when Moonraker has completed metadata indexing for rel_path."""

    async def request_metadata_parse(self, rel_path: str) -> None:
        """Nudge Moonraker to parse metadata for rel_path if supported."""


@dataclass
class SyncResult:
    """Final outcome of waiting for Moonraker to load all USB G-code files."""

    progress: SyncProgress
    seen_paths: set[str]
    ready_paths: set[str]
    cancelled: bool = False
    timed_out: bool = False


ProgressCallback = Callable[[SyncProgress], Awaitable[None]]


def select_newest_file(
    files: Sequence[ScannedGcodeFile],
    available_paths: Optional[set[str]] = None,
) -> Optional[ScannedGcodeFile]:
    """Select the newest G-code file by mtime (deterministic rel_path tie-break)."""
    candidates = list(files)
    if available_paths is not None:
        candidates = [item for item in candidates if item.rel_path in available_paths]
    if not candidates:
        return None
    return max(candidates, key=lambda item: (item.mtime, item.rel_path))


class GcodeSyncEngine:
    """Waits until Moonraker indexes all G-code files on a mounted USB drive."""

    def __init__(self, backend: MoonrakerFileBackend) -> None:
        self._backend = backend
        self._wakeup_event = asyncio.Event()

    def notify_filelist_changed(self) -> None:
        """Wake any active wait barrier when Moonraker emits filelist_changed."""
        self._wakeup_event.set()

    async def wait_for_files_loaded(
        self,
        expected_files: Sequence[ScannedGcodeFile],
        *,
        rel_dir: str,
        wait_for_metadata: bool = True,
        timeout_seconds: float = 60.0,
        poll_interval: float = 0.5,
        on_progress: Optional[ProgressCallback] = None,
        cancel_event: Optional[asyncio.Event] = None,
    ) -> SyncResult:
        """Wait until all expected_files are listed (and metadata-ready if enabled)."""
        expected_paths = [item.rel_path for item in expected_files]
        total = len(expected_paths)

        if total == 0:
            progress = SyncProgress(
                state="ready",
                total_files=0,
                seen_files=0,
                metadata_ready_files=0,
                missing_files=[],
                message="USB mounted (no G-code files found)",
            )
            if on_progress is not None:
                await on_progress(progress)
            return SyncResult(progress=progress, seen_paths=set(), ready_paths=set())

        await self._backend.refresh_directory(rel_dir)

        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.1, float(timeout_seconds))
        seen_paths: set[str] = set()
        ready_paths: set[str] = set()
        nudged_metadata: set[str] = set()
        last_signature: Optional[tuple[str, int, int, int]] = None

        while True:
            if cancel_event is not None and cancel_event.is_set():
                cancelled_progress = SyncProgress(
                    state="idle",
                    total_files=total,
                    seen_files=0,
                    metadata_ready_files=0,
                    missing_files=list(expected_paths),
                    message="USB sync cancelled",
                )
                return SyncResult(
                    progress=cancelled_progress,
                    seen_paths=set(),
                    ready_paths=set(),
                    cancelled=True,
                )

            self._wakeup_event.clear()
            if len(seen_paths) < total:
                listed_files = await self._backend.list_gcode_files()
                seen_paths = {path for path in expected_paths if path in listed_files}

            if wait_for_metadata:
                for path in expected_paths:
                    if path not in seen_paths or path in ready_paths:
                        continue
                    if await self._backend.is_metadata_ready(path):
                        ready_paths.add(path)
                    elif path not in nudged_metadata:
                        nudged_metadata.add(path)
                        await self._backend.request_metadata_parse(path)
            else:
                ready_paths = set(seen_paths)

            missing_files = [path for path in expected_paths if path not in ready_paths]

            if not missing_files:
                done_progress = SyncProgress(
                    state="ready",
                    total_files=total,
                    seen_files=len(seen_paths),
                    metadata_ready_files=len(ready_paths),
                    missing_files=[],
                    message=f"USB ready ({total}/{total} files loaded)",
                )
                if on_progress is not None:
                    await on_progress(done_progress)
                return SyncResult(
                    progress=done_progress,
                    seen_paths=seen_paths,
                    ready_paths=ready_paths,
                )

            now = loop.time()
            if now >= deadline:
                available_for_print = ready_paths if ready_paths else seen_paths
                warning_msg = (
                    f"Timed out waiting for {len(missing_files)}/{total} USB G-code file(s) "
                    "to finish indexing in Moonraker"
                )
                timeout_progress = SyncProgress(
                    state="warning",
                    total_files=total,
                    seen_files=len(seen_paths),
                    metadata_ready_files=len(ready_paths),
                    missing_files=missing_files,
                    message=f"USB partial ({len(available_for_print)}/{total} ready)",
                    warning=warning_msg,
                )
                if on_progress is not None:
                    await on_progress(timeout_progress)
                return SyncResult(
                    progress=timeout_progress,
                    seen_paths=seen_paths,
                    ready_paths=available_for_print,
                    timed_out=True,
                )

            signature = ("syncing", total, len(seen_paths), len(ready_paths))
            if on_progress is not None and signature != last_signature:
                last_signature = signature
                count_now = len(ready_paths) if wait_for_metadata else len(seen_paths)
                await on_progress(
                    SyncProgress(
                        state="syncing",
                        total_files=total,
                        seen_files=len(seen_paths),
                        metadata_ready_files=len(ready_paths),
                        missing_files=missing_files,
                        message=f"USB: Loading {count_now}/{total}",
                    )
                )

            wait_slice = min(poll_interval, max(0.01, deadline - now))
            await self._wait_for_wakeup_or_cancel(wait_slice, cancel_event)

    async def _wait_for_wakeup_or_cancel(
        self,
        timeout: float,
        cancel_event: Optional[asyncio.Event],
    ) -> None:
        if cancel_event is None:
            try:
                await asyncio.wait_for(self._wakeup_event.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                pass
            return

        if cancel_event.is_set():
            return

        wakeup_task = asyncio.create_task(self._wakeup_event.wait())
        cancel_task = asyncio.create_task(cancel_event.wait())
        try:
            _, pending = await asyncio.wait(
                {wakeup_task, cancel_task},
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            for task in pending:
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        except asyncio.CancelledError:
            wakeup_task.cancel()
            cancel_task.cancel()
            raise
