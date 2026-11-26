"""Tests for MountManager scanning, validation, symlink materialization, and cleanup."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from klipper_usb_load.mount_manager import (
    MountError,
    MountManager,
    validate_devnode,
    validate_port_id,
)


def test_validation_rejects_unsafe_devnodes_and_port_ids() -> None:
    assert validate_devnode("/dev/sda1") == "/dev/sda1"
    assert validate_devnode("/dev/sdb") == "/dev/sdb"
    assert validate_port_id("1-1.3") == "1-1.3"

    for bad_dev in ("/dev/nvme0n1", "/dev/sda1;rm", "../sda1", "/tmp/fake"):
        with pytest.raises(MountError):
            validate_devnode(bad_dev)

    for bad_port in ("", "../etc", "1-1/../../etc", "port with spaces"):
        with pytest.raises(MountError):
            validate_port_id(bad_port)


def test_scan_mount_filters_hidden_junk_zero_byte_and_respects_depth(tmp_path: Path) -> None:
    mount_dir = tmp_path / "mnt"
    mount_dir.mkdir()
    gcodes_dir = tmp_path / "gcodes"
    gcodes_dir.mkdir()

    # Valid files at depth 0, 1, 2
    root_gcode = mount_dir / "benchy.gcode"
    root_gcode.write_text("G28\n", encoding="utf-8")
    os.utime(root_gcode, (1000, 1000))

    d1 = mount_dir / "folder1"
    d1.mkdir()
    d1_file = d1 / "part.GCO"
    d1_file.write_text("G1 X10\n", encoding="utf-8")
    os.utime(d1_file, (2000, 2000))

    d2 = d1 / "nested"
    d2.mkdir()
    d2_file = d2 / "deep.g"
    d2_file.write_text("G1 Y10\n", encoding="utf-8")
    os.utime(d2_file, (3000, 3000))

    # Depth 3 should be excluded when max_depth=2
    d3 = d2 / "too_deep"
    d3.mkdir()
    (d3 / "ignored.gcode").write_text("G28\n", encoding="utf-8")

    # Ignored junk files: AppleDouble ._*, hidden dirs, 0-byte, wrong extension
    (mount_dir / "._benchy.gcode").write_text("junk", encoding="utf-8")
    (mount_dir / "empty.gcode").write_text("", encoding="utf-8")
    (mount_dir / "readme.txt").write_text("hello", encoding="utf-8")
    trash_dir = mount_dir / ".Trashes"
    trash_dir.mkdir()
    (trash_dir / "deleted.gcode").write_text("G28\n", encoding="utf-8")
    sys_vol = mount_dir / "System Volume Information"
    sys_vol.mkdir()
    (sys_vol / "sys.gcode").write_text("G28\n", encoding="utf-8")

    manager = MountManager(gcodes_dir=gcodes_dir)
    found = manager.scan_mount_for_gcodes(mount_dir, "1-1.2", max_depth=2)
    rel_paths = [item.rel_path for item in found]

    assert sorted(rel_paths) == [
        "usb/port_1-1.2/benchy.gcode",
        "usb/port_1-1.2/folder1/nested/deep.g",
        "usb/port_1-1.2/folder1/part.GCO",
    ]


def test_materialize_symlinks_and_stale_cleanup(tmp_path: Path) -> None:
    mount_dir = tmp_path / "mnt"
    mount_dir.mkdir()
    (mount_dir / "a.gcode").write_text("G28\n", encoding="utf-8")
    (mount_dir / "sub").mkdir()
    (mount_dir / "sub" / "b.gcode").write_text("G28\n", encoding="utf-8")

    gcodes_dir = tmp_path / "gcodes"
    created_links: list[tuple[str, str]] = []

    def fake_link(src: str, dst: str) -> None:
        created_links.append((src, dst))
        Path(dst).write_text(f"link->{src}", encoding="utf-8")

    manager = MountManager(gcodes_dir=gcodes_dir, link_fn=fake_link)
    files = manager.scan_mount_for_gcodes(mount_dir, "1-1.2", max_depth=2)
    port_dir = manager.materialize_symlinks("1-1.2", files)

    assert port_dir == gcodes_dir / "usb" / "port_1-1.2"
    assert (port_dir / "a.gcode").exists()
    assert (port_dir / "sub" / "b.gcode").exists()
    assert len(created_links) == 2

    # Create an orphaned port folder simulating power loss, plus an unrelated user folder
    stale = gcodes_dir / "usb" / "port_9-9.9"
    stale.mkdir(parents=True)
    (stale / "old.gcode").write_text("G28\n", encoding="utf-8")
    user_folder = gcodes_dir / "usb" / "notes"
    user_folder.mkdir()

    removed = manager.cleanup_stale_links({"1-1.2"})
    assert removed == ["port_9-9.9"]
    assert not stale.exists()
    assert port_dir.exists()
    assert user_folder.exists()
