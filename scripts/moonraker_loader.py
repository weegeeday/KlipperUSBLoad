"""Moonraker component loader shim installed into moonraker/components/klipper_usb_load.py."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any


def _resolve_repo_root() -> Path:
    env_dir = os.environ.get("KLIPPER_USB_LOAD_DIR")
    if env_dir:
        candidate = Path(env_dir).expanduser().resolve()
        if (candidate / "klipper_usb_load").is_dir():
            return candidate

    resolved = Path(__file__).resolve()
    # When symlinked from moonraker/components/klipper_usb_load.py
    repo_candidate = resolved.parent.parent
    if (repo_candidate / "klipper_usb_load").is_dir():
        return repo_candidate

    home_candidate = Path.home() / "KlipperUSBLoad"
    if (home_candidate / "klipper_usb_load").is_dir():
        return home_candidate

    raise ImportError(
        "Unable to locate KlipperUSBLoad package directory. "
        "Set KLIPPER_USB_LOAD_DIR or re-run install.sh."
    )


_REPO_ROOT = str(_resolve_repo_root())
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from klipper_usb_load.component import load_component as _plugin_load_component  # noqa: E402


def load_component(config: Any) -> Any:
    """Moonraker entrypoint forwarding to klipper_usb_load.component.load_component."""
    return _plugin_load_component(config)
