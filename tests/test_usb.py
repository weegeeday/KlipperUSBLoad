"""Tests for sysfs USB storage discovery, scanning, validation, and symlinks."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from klipper_usb_load.usb import (
    MountError,
    UsbMountManager,
    extract_port_id_from_sysfs_path,
    resolve_usb_display_name,
    sanitize_drive_folder_name,
    scan_connected_storage_ports,
    validate_devnode,
    validate_port_id,
)


def test_validation_and_sysfs_path_extraction() -> None:
    assert validate_devnode("/dev/sda1") == "/dev/sda1"
    assert validate_port_id("1-1.3") == "1-1.3"
    assert (
        extract_port_id_from_sysfs_path(
            "/sys/devices/platform/usb1/1-1/1-1.4/1-1.4:1.0/host0/block/sdb/sdb1"
        )
        == "1-1.4"
    )

    for bad_dev in ("/dev/nvme0n1", "/dev/sda1;rm", "../sda1"):
        with pytest.raises(MountError):
            validate_devnode(bad_dev)

    for bad_port in ("", "../etc", "port with spaces"):
        with pytest.raises(MountError):
            validate_port_id(bad_port)


def test_scan_connected_storage_ports_prefers_partitions(tmp_path: Path) -> None:
    sysfs = tmp_path / "sys"
    block_dir = sysfs / "class" / "block"
    block_dir.mkdir(parents=True)

    for name, dev_path in (
        ("sda", "/sys/devices/usb1/1-1/1-1.2/1-1.2:1.0/block/sda"),
        ("sda1", "/sys/devices/usb1/1-1/1-1.2/1-1.2:1.0/block/sda/sda1"),
        ("sdb", "/sys/devices/usb1/1-1/1-1.4/1-1.4:1.0/block/sdb"),
    ):
        entry = block_dir / name
        entry.mkdir()
        (entry / "device_path").write_text(dev_path, encoding="utf-8")

    found = scan_connected_storage_ports(sysfs)
    assert [(item.port_id, item.devnode) for item in found] == [
        ("1-1.2", "/dev/sda1"),
        ("1-1.4", "/dev/sdb"),
    ]


def test_scan_mount_filters_junk_respects_depth_and_materializes(tmp_path: Path) -> None:
    mount_dir = tmp_path / "mnt"
    mount_dir.mkdir()
    gcodes_dir = tmp_path / "gcodes"
    gcodes_dir.mkdir()

    root_gcode = mount_dir / "benchy.gcode"
    root_gcode.write_text("G28\n", encoding="utf-8")
    os.utime(root_gcode, (1000, 1000))

    d1 = mount_dir / "folder1"
    d1.mkdir()
    (d1 / "part.GCO").write_text("G1 X10\n", encoding="utf-8")

    d2 = d1 / "nested"
    d2.mkdir()
    (d2 / "deep.g").write_text("G1 Y10\n", encoding="utf-8")

    d3 = d2 / "too_deep"
    d3.mkdir()
    (d3 / "ignored.gcode").write_text("G28\n", encoding="utf-8")

    (mount_dir / "._benchy.gcode").write_text("junk", encoding="utf-8")
    (mount_dir / "empty.gcode").write_text("", encoding="utf-8")
    (mount_dir / "readme.txt").write_text("hello", encoding="utf-8")
    trash = mount_dir / ".Trashes"
    trash.mkdir()
    (trash / "deleted.gcode").write_text("G28\n", encoding="utf-8")

    created_links: list[tuple[str, str]] = []

    def fake_link(src: str, dst: str) -> None:
        created_links.append((src, dst))
        Path(dst).write_text(f"link->{src}", encoding="utf-8")

    manager = UsbMountManager(gcodes_dir=gcodes_dir, link_fn=fake_link)
    files = manager.scan_mount_for_gcodes(mount_dir, "1-1.2", max_depth=2)
    assert sorted(item.rel_path for item in files) == [
        "usb/port_1-1.2/benchy.gcode",
        "usb/port_1-1.2/folder1/nested/deep.g",
        "usb/port_1-1.2/folder1/part.GCO",
    ]

    port_dir = manager.materialize_symlinks("1-1.2", files)
    assert (port_dir / "benchy.gcode").exists()
    assert (port_dir / "folder1" / "nested" / "deep.g").exists()
    assert len(created_links) == 3

    stale = gcodes_dir / "usb" / "port_9-9.9"
    stale.mkdir(parents=True)
    assert manager.cleanup_stale_links({"1-1.2"}) == ["port_9-9.9"]
    assert not stale.exists()

    # Labeled folders carry the marker file so stale cleanup removes them too
    labeled_dir = manager.materialize_symlinks("1-1.2", [], folder_name="PRUSA_USB")
    assert labeled_dir == gcodes_dir / "usb" / "PRUSA_USB"
    assert (labeled_dir / ".klipper_usb_load").is_file()
    user_folder = gcodes_dir / "usb" / "my_manual_folder"
    user_folder.mkdir()
    assert sorted(manager.cleanup_stale_links(set())) == ["PRUSA_USB", "port_1-1.2"]
    assert not labeled_dir.exists()
    assert user_folder.exists()


def test_resolve_usb_display_name_and_folder_sanitization(tmp_path: Path) -> None:
    sysfs = tmp_path / "sys"
    dev_dir = sysfs / "bus" / "usb" / "devices" / "1-1.2"
    dev_dir.mkdir(parents=True)
    (dev_dir / "product").write_text("SanDisk Ultra Fit", encoding="utf-8")

    # Priority: fs_label -> sysfs product -> port_id
    assert resolve_usb_display_name("1-1.2", fs_label="PRUSA USB", sysfs_root=sysfs) == "PRUSA USB"
    assert resolve_usb_display_name("1-1.2", sysfs_root=sysfs) == "SanDisk Ultra Fit"
    assert resolve_usb_display_name("1-1.9", sysfs_root=sysfs) == "1-1.9"

    assert sanitize_drive_folder_name("PRUSA USB", "1-1.2") == "PRUSA_USB"
    assert sanitize_drive_folder_name("PRUSA USB", "1-1.4", {"PRUSA_USB"}) == "PRUSA_USB_1-1.4"
    assert sanitize_drive_folder_name("...", "1-1.2") == "port_1-1.2"
