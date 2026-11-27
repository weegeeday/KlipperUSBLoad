# KlipperUSBLoad

A lean Klipper/Moonraker plugin that brings plug-in-and-print USB flash drive support directly to your printer's **built-in LCD menu** — configured entirely via `.cfg` / `moonraker.conf` with a **full G-code indexing barrier** so it always waits for Moonraker to load **all** G-code files before picking the newest one.

---

## How It Works

1. **Read-Only Auto-Mount:** Plugging a USB drive into an allowed physical USB port mounts it read-only (`ro,nosuid,nodev,noexec`) without copying files onto your Pi's SD card.
2. **Per-Port Symlinks:** Scans up to `max_scan_depth` folders (ignoring hidden files, `._*` macOS AppleDouble files, and 0-byte files) and links them into `~/printer_data/gcodes/usb/port_<port_id>/`.
3. **Full-Load Readiness Barrier:** Triggers a Moonraker directory refresh and waits until **100% of scanned USB files** appear in Moonraker's file list **and** (when `wait_for_metadata: True`) finish metadata parsing — displaying `USB: Loading X/Y` on the LCD.
4. **LCD Quick-Print:** Once fully loaded, picks the true newest file by modification timestamp, shows `USB Ready: <filename>` on the LCD, and enables the **`USB: <filename>`** entry at the top of the Klipper LCD menu (when idle).

---

## Installation

### Quick Install (One-Liner)

```bash
git clone https://github.com/weegeeday/KlipperUSBLoad.git ~/KlipperUSBLoad && ~/KlipperUSBLoad/install.sh
```

### 1. Include in `~/printer_data/config/printer.cfg`

```ini
[include klipper_usb_load.cfg]
# Include klipper_usb_load_lcd.cfg only if your printer has a [display] section:
[include klipper_usb_load_lcd.cfg]
```

### 2. Configure `[klipper_usb_load]` in `~/printer_data/config/moonraker.conf`

See [`config/moonraker.conf.example`](config/moonraker.conf.example):

```ini
[klipper_usb_load]
# Comma-separated physical USB port IDs (leave empty to allow all USB ports)
# allowed_ports: 1-1.1, 1-1.2
allowed_ports:
max_scan_depth: 2
wait_for_metadata: True
sync_timeout_seconds: 60
gcode_extensions: .gcode, .gco, .g
lcd_notifications: True
usb_subdir_name: usb

[update_manager klipper_usb_load]
type: git_repo
channel: dev
path: ~/KlipperUSBLoad
origin: https://github.com/weegeeday/KlipperUSBLoad.git
primary_branch: main
managed_services: moonraker
install_script: install.sh
```

Restart Klipper and Moonraker after editing the configs.

---

## Configuring Allowed USB Ports (`allowed_ports`)

- Leave `allowed_ports:` empty to accept USB flash drives in any physical USB slot.
- To restrict to specific physical USB slots (for example, only the front-panel USB port), set `allowed_ports: 1-1.1` (or comma-separated `1-1.1, 1-1.2`).
- **Finding a port ID:** Set a dummy port like `allowed_ports: none`, restart Moonraker, and plug your flash drive into the slot you want to allow — the LCD will display `USB blocked: 1-1.1` and `moonraker.log` will log the exact physical port ID (`1-1.1`) to copy into `moonraker.conf`.

---

## Running Tests

```bash
python -m pip install -e ".[dev]"
pytest -v
```
