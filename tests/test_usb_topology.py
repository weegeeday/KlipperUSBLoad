"""Tests for physical USB port topology discovery via simulated sysfs."""

from __future__ import annotations

from pathlib import Path

from klipper_usb_load.models import MountedDriveState, PluginSettings
from klipper_usb_load.usb_topology import (
    UsbTopologyScanner,
    extract_port_id_from_sysfs_path,
    sanitize_port_id,
)


def _create_fake_usb_device(
    usb_devices_dir: Path,
    name: str,
    *,
    iface_class: str = "",
    device_class: str = "",
    maxchild: int = 0,
    manufacturer: str = "",
    product: str = "",
    serial: str = "",
) -> None:
    dev_dir = usb_devices_dir / name
    dev_dir.mkdir(parents=True, exist_ok=True)
    if device_class:
        (dev_dir / "bDeviceClass").write_text(device_class, encoding="utf-8")
    if maxchild:
        (dev_dir / "maxchild").write_text(str(maxchild), encoding="utf-8")
    if manufacturer:
        (dev_dir / "manufacturer").write_text(manufacturer, encoding="utf-8")
    if product:
        (dev_dir / "product").write_text(product, encoding="utf-8")
    if serial:
        (dev_dir / "serial").write_text(serial, encoding="utf-8")

    if iface_class:
        iface_dir = dev_dir / "iface_1.0"
        iface_dir.mkdir(parents=True, exist_ok=True)
        (iface_dir / "bInterfaceClass").write_text(iface_class, encoding="utf-8")


def test_sysfs_topology_discovers_storage_mcu_hub_and_empty_ports(tmp_path: Path) -> None:
    sysfs = tmp_path / "sys"
    usb_devices = sysfs / "bus" / "usb" / "devices"
    block_dir = sysfs / "class" / "block"
    usb_devices.mkdir(parents=True)
    block_dir.mkdir(parents=True)

    # Root hub usb1 with 2 root ports; 1-1 is an internal 4-port hub
    _create_fake_usb_device(usb_devices, "usb1", device_class="09", maxchild=2)
    _create_fake_usb_device(usb_devices, "1-1", device_class="09", iface_class="09", maxchild=4)

    # Port 1-1.2 has a USB flash drive (sda + sda1)
    _create_fake_usb_device(
        usb_devices,
        "1-1.2",
        iface_class="08",
        manufacturer="SanDisk",
        product="Ultra Fit",
        serial="SN123",
    )
    (block_dir / "sda").mkdir()
    (block_dir / "sda" / "device_path").write_text(
        "/sys/devices/platform/scb/usb1/1-1/1-1.2/1-1.2:1.0/host0/target0:0:0/0:0:0:0/block/sda",
        encoding="utf-8",
    )
    (block_dir / "sda1").mkdir()
    (block_dir / "sda1" / "device_path").write_text(
        "/sys/devices/platform/scb/usb1/1-1/1-1.2/1-1.2:1.0/host0/target0:0:0/0:0:0:0/block/sda/sda1",
        encoding="utf-8",
    )

    # Port 1-1.3 has a Klipper STM32 MCU (CDC-ACM class 02)
    _create_fake_usb_device(
        usb_devices,
        "1-1.3",
        iface_class="02",
        manufacturer="STMicroelectronics",
        product="Klipper STM32",
    )

    # Port 1-2 is empty, 1-1.1 and 1-1.4 are empty, and 2-1 is configured in settings but unplugged
    settings = PluginSettings(
        port_policy="allowlist",
        allowed_ports=["1-1.2", "2-1"],
        port_labels={"1-1.2": "Front Slot", "2-1": "Side Slot"},
    )
    mounted = {
        "1-1.2": MountedDriveState(
            port_id="1-1.2",
            devnode="/dev/sda1",
            mount_point="/run/klipper-usb-load/1-1.2",
            gcode_subdir="usb/port_1-1.2",
            fs_label="GCODES",
            fs_type="vfat",
        )
    }

    scanner = UsbTopologyScanner(sysfs)
    ports = scanner.scan_ports(settings=settings, mounted_drives=mounted)
    by_id = {p.port_id: p for p in ports}

    assert [p.port_id for p in ports] == ["1-1.1", "1-1.2", "1-1.3", "1-1.4", "1-2", "2-1"]

    assert by_id["1-1.2"].device_class == "storage"
    assert by_id["1-1.2"].devnode == "/dev/sda1"
    assert by_id["1-1.2"].mounted is True
    assert by_id["1-1.2"].allowed is True
    assert by_id["1-1.2"].label == "Front Slot"
    assert by_id["1-1.2"].fs_label == "GCODES"

    assert by_id["1-1.3"].device_class == "non_storage"
    assert by_id["1-1.3"].allowed is False

    assert by_id["1-1.1"].connected is False
    assert by_id["1-1.1"].device_class == "empty"

    assert by_id["2-1"].connected is False
    assert by_id["2-1"].allowed is True
    assert by_id["2-1"].label == "Side Slot"


def test_path_helpers() -> None:
    assert (
        extract_port_id_from_sysfs_path(
            "/devices/platform/usb1/1-1/1-1.4/1-1.4:1.0/host0/block/sdb/sdb1"
        )
        == "1-1.4"
    )
    assert sanitize_port_id("1-1.4") == "1-1.4"
    assert sanitize_port_id("1-1:a") == "1-1_a"
