#!/usr/bin/env bash
# uninstall.sh — Remove aibt service registration and crontab entry.
# Does NOT delete config files, logs, or data.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

log() { echo "[uninstall] $*"; }

get_env() {
  local key="$1" default="${2:-}"
  local val
  val=$(grep -E "^\s*${key}\s*=" .env 2>/dev/null | head -1 \
        | sed "s/^\s*${key}\s*=\s*//" | sed 's/[[:space:]]*#.*//' | tr -d '\r')
  echo "${val:-$default}"
}

AIBT_INSTANCE=$(get_env "AIBT_INSTANCE" "aibt")
SERVICE="${AIBT_INSTANCE}"
IS_ROOT=false
[[ $EUID -eq 0 ]] && IS_ROOT=true

log "Instance: ${AIBT_INSTANCE}"

# ── systemd service ───────────────────────────────────────────────────────────
stop_service() {
  local sd_args=()
  if ! $IS_ROOT; then sd_args=("--user"); fi

  if systemctl "${sd_args[@]}" is-active "$SERVICE" 2>/dev/null | grep -q "^active"; then
    log "Stopping ${SERVICE}..."
    systemctl "${sd_args[@]}" stop "$SERVICE" 2>/dev/null || true
  fi

  if systemctl "${sd_args[@]}" is-enabled "$SERVICE" 2>/dev/null | grep -q "enabled"; then
    log "Disabling ${SERVICE}..."
    systemctl "${sd_args[@]}" disable "$SERVICE" 2>/dev/null || true
  fi

  # Remove unit file
  local paths=()
  if $IS_ROOT; then
    paths=("/etc/systemd/system/${SERVICE}.service")
  else
    paths=(
      "${HOME}/.config/systemd/user/${SERVICE}.service"
      "/etc/systemd/system/${SERVICE}.service"
    )
  fi
  for p in "${paths[@]}"; do
    if [[ -f "$p" ]]; then
      log "Removing ${p}..."
      rm -f "$p"
    fi
  done

  systemctl "${sd_args[@]}" daemon-reload 2>/dev/null || true
  log "Service removed."
}

# ── crontab entry ─────────────────────────────────────────────────────────────
remove_crontab() {
  local cron_py="${SCRIPT_DIR}/src/core/cron.py"
  local current
  current=$(crontab -l 2>/dev/null || true)
  if echo "$current" | grep -q "$cron_py"; then
    log "Removing cron.py from crontab..."
    echo "$current" | grep -v "$cron_py" | crontab - 2>/dev/null || true
    log "Crontab entry removed."
  else
    log "No crontab entry found for cron.py."
  fi
}

stop_service
remove_crontab

echo
echo "───────────────────────────────────────────────"
echo " aibt service uninstalled."
echo " Config, logs, and data were NOT deleted."
echo " To reinstall: ./install.sh"
echo "───────────────────────────────────────────────"
