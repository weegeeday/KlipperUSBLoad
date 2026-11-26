#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET_USER="${SUDO_USER:-$(id -un)}"
MOONRAKER_PATH="${HOME}/moonraker"
PRINTER_DATA_PATH="${HOME}/printer_data"
SYSTEM_PREFIX=""
NON_INTERACTIVE=0
WITH_LCD=0
WITH_NGINX=0

usage() {
  cat <<EOF
Usage: ./install.sh [options]

Options:
  --non-interactive       Do not prompt for confirmation
  --with-lcd              Copy optional klipper_usb_load_lcd.cfg to printer_data/config
  --with-nginx            Install nginx snippet into /etc/nginx/conf.d/klipper-usb-load.conf
  --moonraker-path PATH   Path to Moonraker checkout (default: ~/moonraker)
  --printer-data PATH     Path to printer_data directory (default: ~/printer_data)
  --system-prefix PATH    Install system files under PATH instead of / (for testing)
  --user NAME             System user running Moonraker (default: current user)
  -h, --help              Show this help message
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --non-interactive)
      NON_INTERACTIVE=1
      shift
      ;;
    --with-lcd)
      WITH_LCD=1
      shift
      ;;
    --with-nginx)
      WITH_NGINX=1
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
    --user)
      TARGET_USER="$2"
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

install_file_priv() {
  local mode="$1"
  local src="$2"
  local dst="$3"
  run_priv install -d "$(dirname "$dst")"
  run_priv install -m "$mode" "$src" "$dst"
}

main() {
  if [[ "$NON_INTERACTIVE" -eq 0 ]]; then
    echo "Installing KlipperUSBLoad for user '${TARGET_USER}'..."
  fi

  local components_dir="${MOONRAKER_PATH}/moonraker/components"
  local config_dir="${PRINTER_DATA_PATH}/config"
  local gcodes_dir="${PRINTER_DATA_PATH}/gcodes"

  mkdir -p "$components_dir" "$config_dir" "$gcodes_dir"

  # 1. Symlink Moonraker component shim and keep Moonraker git clean
  ln -sfn "${SCRIPT_DIR}/scripts/moonraker_loader.py" "${components_dir}/klipper_usb_load.py"
  local exclude_file="${MOONRAKER_PATH}/.git/info/exclude"
  if [[ -d "${MOONRAKER_PATH}/.git" ]]; then
    mkdir -p "$(dirname "$exclude_file")"
    if [[ ! -f "$exclude_file" ]] || ! grep -qxF "moonraker/components/klipper_usb_load.py" "$exclude_file"; then
      printf '%s\n' "moonraker/components/klipper_usb_load.py" >> "$exclude_file"
    fi
  fi

  # 2. Copy Klipper configs
  install -m 0644 "${SCRIPT_DIR}/config/klipper_usb_load.cfg" "${config_dir}/klipper_usb_load.cfg"
  if [[ "$WITH_LCD" -eq 1 ]]; then
    install -m 0644 "${SCRIPT_DIR}/config/klipper_usb_load_lcd.cfg" "${config_dir}/klipper_usb_load_lcd.cfg"
  fi

  # 3. Install system mount helper, udev notifier, udev rule, tmpfiles, and sudoers
  local helper_dst="${SYSTEM_PREFIX}/usr/local/sbin/klipper-usb-load-helper"
  local notify_dst="${SYSTEM_PREFIX}/usr/local/bin/klipper-usb-load-notify"
  local udev_dst="${SYSTEM_PREFIX}/etc/udev/rules.d/99-klipper-usb-load.rules"
  local tmpfiles_dst="${SYSTEM_PREFIX}/etc/tmpfiles.d/klipper-usb-load.conf"
  local sudoers_dst="${SYSTEM_PREFIX}/etc/sudoers.d/klipper-usb-load"

  install_file_priv 0755 "${SCRIPT_DIR}/scripts/klipper-usb-load-helper" "$helper_dst"
  install_file_priv 0755 "${SCRIPT_DIR}/scripts/klipper-usb-load-notify" "$notify_dst"
  install_file_priv 0644 "${SCRIPT_DIR}/scripts/99-klipper-usb-load.rules" "$udev_dst"

  local tmp_tmpfiles
  tmp_tmpfiles="$(mktemp)"
  sed "s/@USER@/${TARGET_USER}/g" "${SCRIPT_DIR}/scripts/klipper-usb-load.tmpfiles.template" > "$tmp_tmpfiles"
  install_file_priv 0644 "$tmp_tmpfiles" "$tmpfiles_dst"
  rm -f "$tmp_tmpfiles"

  local tmp_sudoers
  tmp_sudoers="$(mktemp)"
  sed "s/@USER@/${TARGET_USER}/g" "${SCRIPT_DIR}/scripts/klipper-usb-load.sudoers.template" > "$tmp_sudoers"
  if command -v visudo >/dev/null 2>&1; then
    visudo -cf "$tmp_sudoers" >/dev/null
  fi
  install_file_priv 0440 "$tmp_sudoers" "$sudoers_dst"
  rm -f "$tmp_sudoers"

  if [[ "$WITH_NGINX" -eq 1 ]]; then
    local nginx_dst="${SYSTEM_PREFIX}/etc/nginx/conf.d/klipper-usb-load.conf"
    install_file_priv 0644 "${SCRIPT_DIR}/config/nginx-klipper-usb-load.conf" "$nginx_dst"
  fi

  if [[ -z "$SYSTEM_PREFIX" ]]; then
    if command -v systemd-tmpfiles >/dev/null 2>&1; then
      run_priv systemd-tmpfiles --create /etc/tmpfiles.d/klipper-usb-load.conf || true
    else
      run_priv mkdir -p /run/klipper-usb-load
      run_priv chown "${TARGET_USER}:${TARGET_USER}" /run/klipper-usb-load || true
    fi
    if command -v udevadm >/dev/null 2>&1; then
      run_priv udevadm control --reload-rules || true
    fi
    if command -v systemctl >/dev/null 2>&1 && systemctl list-unit-files moonraker.service >/dev/null 2>&1; then
      run_priv systemctl restart moonraker || true
    fi
  fi

  echo "KlipperUSBLoad installed successfully."
  echo "Next steps:"
  echo "  1. Add [include klipper_usb_load.cfg] to ${config_dir}/printer.cfg"
  if [[ "$WITH_LCD" -eq 1 ]]; then
    echo "     and [include klipper_usb_load_lcd.cfg] if you have a [display] section"
  fi
  echo "  2. Add [klipper_usb_load] to ${config_dir}/moonraker.conf"
  echo "  3. Open Settings Page at http://<printer-ip>:7125/server/klipper_usb_load/ui/"
}

main
