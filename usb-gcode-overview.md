# USB G-Code Auto-Mount & Quick-Print Overview

## Purpose

This feature brings a traditional "plug-in-and-print" workflow to a Klipper-based 3D printer. Instead of uploading G-code files over a network or manually copying them onto the Raspberry Pi, a user can plug a USB flash drive directly into the Pi, immediately see the newest G-code file on the printer's built-in screen, and start printing it from the physical menu.

---

## High-Level Architecture

The system connects four layers of the printer's software stack:

1. **Operating System Device Detection** — Watches for a USB storage device being plugged in or unplugged.
2. **Background Mount & Sync Service** — Connects the USB drive to the filesystem, surfaces its G-code files to Klipper, and identifies the most recent file.
3. **Printer API Bridge** — Passes the newest filename and status messages to the running printer firmware controller.
4. **Built-In LCD Menu** — Displays the filename on the status line and exposes a quick-start menu option when a valid file is ready.

---

## How It Works

### 1. Inserting a USB Drive

- **Automatic Detection:** When a USB flash drive is plugged into the Raspberry Pi, the Linux device manager recognizes the new storage partition and triggers a dedicated background service.
- **Mounting Without Copying:** The service mounts the USB drive to a temporary folder and scans its top-level directories for G-code files. Rather than copying those files onto the Pi's SD card, it creates lightweight filesystem shortcuts pointing from Klipper's G-code folder to the files on the USB drive.
- **Finding the Newest File:** While scanning, the service checks modification timestamps to identify the most recently saved G-code file on the drive.
- **Updating the Display:** The service sends that filename to the printer's API server (Moonraker), which stores it in memory and posts a brief notification to the printer's built-in LCD screen.

### 2. Printing from the Built-In Screen

- **Dynamic Menu Entry:** The printer's LCD menu includes a dedicated "Print USB" option at the top of the main menu.
- **Context-Aware Visibility:** This option is only enabled when the printer is idle and a USB G-code file has been detected. Selecting it immediately starts printing the newest file without needing a phone, browser, or computer.
- **Full Library Access:** Because all G-code files on the drive are linked into Klipper's G-code directory, older files on the same USB stick are also available through the normal web interface or file browser.

### 3. Removing the USB Drive

- **Automatic Cleanup:** Unplugging the USB drive triggers the service's cleanup routine.
- **Resetting State:** Any shortcuts pointing to the removed drive are deleted, the mount point is released, the stored filename is cleared, the quick-print menu option is hidden, and the LCD status line updates to confirm the drive was removed.

---

## Key Design Benefits & Trade-Offs

| Aspect | Benefit / Consideration |
| :--- | :--- |
| **No SD Card Wear** | Files are linked directly from the USB drive instead of being copied onto the Pi's storage. |
| **Fastest Common Case** | Slicing a part, putting it on a USB stick, and plugging it in puts that exact file one click away on the LCD. |
| **Live Dependency** | Because files are read directly from the USB drive, the stick must remain plugged in for the duration of the print. |
| **Single-Drive Assumption** | The mount point is designed for one active USB G-code drive at a time. |
