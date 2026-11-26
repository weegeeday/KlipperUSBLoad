#!/usr/bin/env bash
set -euo pipefail

MOONRAKER_PATH="${HOME}/moonraker"
PRINTER_DATA_PATH="${HOME}/printer_data"
SYSTEM_PREFIX=""
NON_INTERACTIVE=0

usage() {
  cat <<EOF
Usage: ./uninstall.sh [options]

Options:
  --non-interactive       Do not prompt for confirmation
  --moonraker-path PATH   Path to Moonraker checkout (default: ~/moonraker)
  --printer-data PATH     Path to printer_data directory (default: ~/printer_data)
  --system-prefix PATH    Remove system files under PATH instead of / (for testing)
  -h, --help              Show this help message
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --non-interactive)
      NON_INTERACTIVE=1
      shift
      ;;
    --moonraker-path)
      MOONRAKER_PATH="$2"
      shift 2
      ;;
    --printer-data)
      PRINTER_DATA_PATH="$2"
      shift 2
      ;;
    --system-prefix)
      SYSTEM_PREFIX="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

run_priv() {
  if [[ -n "$SYSTEM_PREFIX" || "$(id -u)" -eq 0 ]]; then
    "$@"
  else
    sudo "$@"
  fi
}

main() {
  if [[ "$NON_INTERACTIVE" -eq 0 ]]; then
    echo "Uninstalling KlipperUSBLoad..."
  fi

  local helper_dst="${SYSTEM_PREFIX}/usr/local/sbin/klipper-usb-load-helper"
  local notify_dst="${SYSTEM_PREFIX}/usr/local/bin/klipper-usb-load-notify"
  local udev_dst="${SYSTEM_PREFIX}/etc/udev/rules.d/99-klipper-usb-load.rules"
  local sudoers_dst="${SYSTEM_PREFIX}/etc/sudoers.d/klipper-usb-load"
  local nginx_dst="${SYSTEM_PREFIX}/etc/nginx/conf.d/klipper-usb-load.conf"

  if [[ -z "$SYSTEM_PREFIX" && -x "$helper_dst" ]]; then
    run_priv "$helper_dst" cleanup-all || true
  fi

  rm -f "${MOONRAKER_PATH}/moonraker/components/klipper_usb_load.py"
  rm -rf "${PRINTER_DATA_PATH}/gcodes/usb"

  run_priv rm -f "$helper_dst" "$notify_dst" "$udev_dst" "$sudoers_dst" "$nginx_dst"

  if [[ -z "$SYSTEM_PREFIX" ]]; then
    if command -v udevadm >/dev/null 2>&1; then
      run_priv udevadm control --reload-rules || true
    fi
    if command -v systemctl >/dev/null 2>&1 && systemctl list-unit-files moonraker.service >/dev/null 2>&1; then
      run_priv systemctl restart moonraker || true
    fi
  fi

  echo "KlipperUSBLoad uninstalled cleanly (user config in ${PRINTER_DATA_PATH}/config preserved)."
}

main
