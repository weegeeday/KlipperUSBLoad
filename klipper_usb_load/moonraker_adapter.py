"""Moonraker component adapter for file_manager, klippy_apis, and LCD macros."""

from __future__ import annotations

import inspect
import logging
import re
from typing import Any, Optional

from .models import ActivePrintInfo, ScannedGcodeFile

LOGGER = logging.getLogger("moonraker.klipper_usb_load")

_GCODE_CTRL_RE = re.compile(r"[\r\n\x00]+")


def sanitize_gcode_string(value: str, max_len: int = 180) -> str:
    """Strip control chars and double quotes so values are safe inside G-code strings."""
    cleaned = _GCODE_CTRL_RE.sub(" ", value).replace('"', "'").strip()
    return cleaned[:max_len]


class MoonrakerAdapter:
    """Wraps Moonraker's file_manager and klippy_apis across Moonraker versions."""

    def __init__(self, server: Any) -> None:
        self.server = server

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

    async def refresh_directory(self, rel_dir: str) -> None:
        """Ask Moonraker file_manager to rescan rel_dir inside gcodes/."""
        file_manager = self._get_component("file_manager")
        if file_manager is None:
            return

        for method_name in ("refresh_directory", "sync_dir", "schedule_sync_dir"):
            method = getattr(file_manager, method_name, None)
            if method is None:
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
        if get_file_list is None:
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
            return {
                str(k): (v if isinstance(v, dict) else {})
                for k, v in raw.items()
            }

        if isinstance(raw, list):
            mapped: dict[str, dict[str, Any]] = {}
            for item in raw:
                if isinstance(item, dict) and "path" in item:
                    mapped[str(item["path"])] = item
            return mapped

        return {}

    async def is_metadata_ready(self, rel_path: str) -> bool:
        """Return True if Moonraker has metadata stored for rel_path."""
        file_manager = self._get_component("file_manager")
        if file_manager is None:
            return False

        gcode_metadata = getattr(file_manager, "gcode_metadata", None)
        if gcode_metadata is not None:
            getter = getattr(gcode_metadata, "get", None)
            if callable(getter):
                try:
                    meta = getter(rel_path)
                    if inspect.isawaitable(meta):
                        meta = await meta
                    if isinstance(meta, dict) and bool(meta):
                        return True
                except Exception:
                    pass

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

        listed = await self.list_gcode_files()
        entry = listed.get(rel_path)
        if not entry:
            return False
        if entry.get("metadata_ready") is True or entry.get("metadata_pulled") is True:
            return True
        return any(
            key in entry
            for key in ("slicer", "estimated_time", "filament_total", "layer_height", "thumbnails")
        )

    async def request_metadata_parse(self, rel_path: str) -> None:
        """Trigger Moonraker metadata parsing for rel_path if the API exposes it."""
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

    async def get_active_print_info(self) -> ActivePrintInfo:
        """Query Klipper's print_stats to see if a print is currently active."""
        klippy_apis = self._get_component("klippy_apis")
        if klippy_apis is None:
            return ActivePrintInfo()

        query_objects = getattr(klippy_apis, "query_objects", None)
        if not callable(query_objects):
            return ActivePrintInfo()

        try:
            result = query_objects({"print_stats": ["state", "filename"]})
            if inspect.isawaitable(result):
                result = await result
            if isinstance(result, dict):
                stats = result.get("print_stats", {})
                if isinstance(stats, dict):
                    return ActivePrintInfo(
                        state=str(stats.get("state", "standby")),
                        filename=str(stats.get("filename", "")),
                    )
        except Exception as err:
            LOGGER.debug("Could not query Klipper print_stats: %s", err)
        return ActivePrintInfo()

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
            LOGGER.debug("run_gcode(%s) skipped/failed: %s", script, err)

    async def start_print(self, rel_path: str) -> None:
        """Start printing rel_path via Moonraker klippy_apis."""
        klippy_apis = self._get_component("klippy_apis")
        if klippy_apis is None:
            raise RuntimeError("klippy_apis component is not available")

        start_print = getattr(klippy_apis, "start_print", None)
        if callable(start_print):
            result = start_print(rel_path)
            if inspect.isawaitable(result):
                await result
            return

        safe_path = sanitize_gcode_string(rel_path, max_len=240)
        await self.run_gcode(f'SDCARD_PRINT_FILE FILENAME="{safe_path}"')

    async def publish_usb_state(
        self,
        *,
        newest_file: Optional[ScannedGcodeFile],
        total_count: int,
        status_text: str,
        notify_msg: Optional[str] = None,
        lcd_notifications: bool = True,
    ) -> None:
        """Update Klipper _USB_LOAD_STATE macro variables and optionally show LCD message."""
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
                await self.run_gcode(f'_USB_LOAD_NOTIFY MSG="{safe_msg}"')
