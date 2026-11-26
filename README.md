# KlipperUSBLoad

A Moonraker/Klipper plugin that brings true plug-in-and-print USB flash drive support to Klipper printers — with **physical USB port filtering**, a **built-in Web Settings Page**, and a **full G-code indexing barrier** that waits until Moonraker/Mainsail has seen and indexed **all** G-code files on the drive before picking the newest one.

---

## Why KlipperUSBLoad?

A common pitfall with naive USB auto-mount scripts is picking the "newest" G-code file immediately on USB insertion or polling too early while Moonraker/Mainsail is still scanning the directory and parsing metadata. That causes stale/older files to be selected or print starts to fail before Moonraker has finished loading the files.

KlipperUSBLoad fixes this with a two-phase readiness barrier:

1. **Direct Drive Scan:** Immediately scans the read-only mounted USB drive (`ro,nosuid,nodev,noexec`) to build a ground-truth manifest of all valid G-code files and their modification timestamps.
2. **Per-Port Symlink Exposure:** Links files into `~/printer_data/gcodes/usb/port_<port_id>/` without copying onto the SD card, isolating multiple drives and preventing filename collisions.
3. **Full-Load Readiness Barrier:** Triggers Moonraker's `file_manager` refresh and waits until **100% of expected USB files** appear in Moonraker's file list **and** (when `wait_for_metadata` is enabled) finish metadata parsing — showing live `"USB: Loading X/Y"` progress on the LCD and Settings UI.
4. **True Newest Selection:** Only after the barrier completes does it pick the newest G-code file and arm Klipper's `PRINT_NEWEST_USB` macro and optional LCD menu entry.

---

## Features

- **Web Settings Page** served directly by Moonraker at `http://<printer-ip>:7125/server/klipper_usb_load/ui/` (or `/usb-load/` via optional Nginx proxy).
- **Physical USB Port Allowlist:** Filter by Linux sysfs physical USB port topology (`1-1.2`, `1-1.3`, etc.) so only chosen USB slots auto-mount (while ignoring Klipper MCUs, webcams, or blocked slots).
- **Interactive "Detect Port" Helper:** Click **Detect Port**, plug a flash drive into the slot you want to configure, give it a custom label, and save it to the allowlist.
- **Multi-Drive & Active Print Protection:** Supports multiple allowed ports simultaneously, blocks UI unmount while a drive is actively printing, and warns loudly on LCD/UI if a drive is physically pulled mid-print.
- **Moonraker `update_manager` Ready:** Installs cleanly via symlink + `.git/info/exclude` without leaving `~/moonraker` dirty.

---

## Installation

### Quick Install (One-Liner)

```bash
git clone https://github.com/weegeeday/KlipperUSBLoad.git ~/KlipperUSBLoad && ~/KlipperUSBLoad/install.sh --with-lcd
```

*(Omit `--with-lcd` on headless or touchscreen-only printers that do not have a Klipper `[display]` section.)*

### Step-by-Step / Options

```bash
cd ~
git clone https://github.com/weegeeday/KlipperUSBLoad.git
cd KlipperUSBLoad
./install.sh --with-lcd
```

Options for `./install.sh`:
- `--with-lcd`: Also copies `klipper_usb_load_lcd.cfg` (for printers with a physical `[display]`).
- `--with-nginx`: Installs `/etc/nginx/conf.d/klipper-usb-load.conf` for `/usb-load/` reverse proxy.
- `--non-interactive`: Skips prompts (used by Moonraker `update_manager`).

### 1. Update `~/printer_data/config/printer.cfg`

```ini
[include klipper_usb_load.cfg]
# Optional — only if your printer has a [display] section:
[include klipper_usb_load_lcd.cfg]
```

### 2. Update `~/printer_data/config/moonraker.conf`

See [`config/moonraker.conf.example`](config/moonraker.conf.example):

```ini
[klipper_usb_load]

[update_manager klipper_usb_load]
type: git_repo
channel: dev
path: ~/KlipperUSBLoad
origin: https://github.com/weegeeday/KlipperUSBLoad.git
primary_branch: main
managed_services: moonraker
install_script: install.sh
```

Restart Klipper and Moonraker, then open:
`http://<printer-ip>:7125/server/klipper_usb_load/ui/`

---

## Configuration & Settings Persistence

Live settings from the Web Settings Page are stored atomically in:
`~/printer_data/config/klipper_usb_load.json`

| Setting | Default | Description |
| :--- | :--- | :--- |
| `port_policy` | `"allow_all"` | `"allow_all"` or `"allowlist"` (only checked physical ports mount). |
| `allowed_ports` | `[]` | Physical USB port IDs (e.g. `["1-1.2"]`) permitted when `port_policy` is `"allowlist"`. |
| `port_labels` | `{}` | Optional friendly names per physical port ID. |
| `wait_for_metadata` | `true` | Wait for Moonraker metadata parsing on every USB file (disable for file-list-only mode). |
| `sync_timeout_seconds` | `60.0` | Timeout before falling back to partial ready with a warning. |
| `max_scan_depth` | `2` | Folder depth scanned on USB drives (`0` = root only). |
| `gcode_extensions` | `[".gcode", ".gco", ".g"]` | File extensions recognized as G-code. |
| `lcd_notifications` | `true` | Show loading/ready/removal status on Klipper's `M117` line. |

---

## Security Model

- USB drives are mounted **read-only** with `ro,nosuid,nodev,noexec`.
- Privileged mounting runs through `/usr/local/sbin/klipper-usb-load-helper`, which strictly validates `/dev/sd*` block nodes and `/run/klipper-usb-load/*` mount targets.
- Udev notifications authenticate to Moonraker with a local token generated at `/run/klipper-usb-load/udev.token`.
- Symlinks escaping the mounted USB root are rejected, and hidden/AppleDouble (`._*`) junk files are ignored.

---

## Running Tests Locally

```bash
python -m pip install -e ".[dev]"
pytest -v
node --test tests/test_frontend.mjs
```
