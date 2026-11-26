#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

MOONRAKER_DIR="${TMP_DIR}/moonraker"
PRINTER_DATA_DIR="${TMP_DIR}/printer_data"
SYS_PREFIX="${TMP_DIR}/sys_prefix"

mkdir -p "${MOONRAKER_DIR}/.git/info" "${MOONRAKER_DIR}/moonraker/components" "${PRINTER_DATA_DIR}/config" "${PRINTER_DATA_DIR}/gcodes"

"${REPO_ROOT}/install.sh" \
  --non-interactive \
  --with-lcd \
  --with-nginx \
  --moonraker-path "$MOONRAKER_DIR" \
  --printer-data "$PRINTER_DATA_DIR" \
  --system-prefix "$SYS_PREFIX" \
  --user "pi"

test -L "${MOONRAKER_DIR}/moonraker/components/klipper_usb_load.py"
grep -qxF "moonraker/components/klipper_usb_load.py" "${MOONRAKER_DIR}/.git/info/exclude"
test -f "${PRINTER_DATA_DIR}/config/klipper_usb_load.cfg"
test -f "${PRINTER_DATA_DIR}/config/klipper_usb_load_lcd.cfg"
test -x "${SYS_PREFIX}/usr/local/sbin/klipper-usb-load-helper"
test -x "${SYS_PREFIX}/usr/local/bin/klipper-usb-load-notify"
test -f "${SYS_PREFIX}/etc/udev/rules.d/99-klipper-usb-load.rules"
test -f "${SYS_PREFIX}/etc/sudoers.d/klipper-usb-load"
test -f "${SYS_PREFIX}/etc/nginx/conf.d/klipper-usb-load.conf"
grep -q "pi ALL=(root) NOPASSWD:" "${SYS_PREFIX}/etc/sudoers.d/klipper-usb-load"

# Verify loader shim resolves package cleanly from symlinked component path
python3 -c "
import importlib.util
spec = importlib.util.spec_from_file_location('klipper_usb_load_shim', '${MOONRAKER_DIR}/moonraker/components/klipper_usb_load.py')
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
assert callable(mod.load_component)
"

# Verify uninstall cleans links and system files while keeping user config
"${REPO_ROOT}/uninstall.sh" \
  --non-interactive \
  --moonraker-path "$MOONRAKER_DIR" \
  --printer-data "$PRINTER_DATA_DIR" \
  --system-prefix "$SYS_PREFIX"

test ! -e "${MOONRAKER_DIR}/moonraker/components/klipper_usb_load.py"
test ! -e "${SYS_PREFIX}/usr/local/sbin/klipper-usb-load-helper"
test ! -e "${SYS_PREFIX}/usr/local/bin/klipper-usb-load-notify"
test ! -e "${SYS_PREFIX}/etc/udev/rules.d/99-klipper-usb-load.rules"
test ! -e "${SYS_PREFIX}/etc/sudoers.d/klipper-usb-load"
test -f "${PRINTER_DATA_DIR}/config/klipper_usb_load.cfg"

echo "install/uninstall dry-run passed!"
