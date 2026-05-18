#!/usr/bin/env bash
# install.sh — Install or update aibt.
# Usage: ./install.sh [config-template]
#   config-template: optional path or name of a config.json5.example variant.
#                    Extension .json5.example may be omitted.
#                    If omitted, config.json5.example is used.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ── Helpers ───────────────────────────────────────────────────────────────────

log() { echo "[install] $*"; }

# Read a key from .env (file in current directory).
get_env() {
  local key="$1" default="${2:-}"
  local val
  val=$(grep -E "^\s*${key}\s*=" .env 2>/dev/null | head -1 \
        | sed "s/^\s*${key}\s*=\s*//" | sed 's/[[:space:]]*#.*//' | tr -d '\r')
  echo "${val:-$default}"
}

# ── Step 1: Create .env from .env.example on first install ───────────────────
FIRST_INSTALL=false
if [[ ! -f .env ]]; then
  FIRST_INSTALL=true
  log "Creating .env from .env.example..."
  cp .env.example .env

  # Set AIBT_ROOT to the current directory
  sed -i "s|AIBT_ROOT=.*|AIBT_ROOT=${SCRIPT_DIR}|" .env
  log "AIBT_ROOT set to ${SCRIPT_DIR}"

  # Find a unique AIBT_INSTANCE name
  BASE_INSTANCE=$(get_env "AIBT_INSTANCE" "aibt")
  INSTANCE="$BASE_INSTANCE"
  COUNTER=2
  while true; do
    if systemctl --user is-active "${INSTANCE}" 2>/dev/null | grep -q "^active" ||
       systemctl is-active "${INSTANCE}" 2>/dev/null | grep -q "^active"; then
      INSTANCE="${BASE_INSTANCE}${COUNTER}"
      ((COUNTER++))
    else
      break
    fi
  done
  if [[ "$INSTANCE" != "$BASE_INSTANCE" ]]; then
    sed -i "s|AIBT_INSTANCE=.*|AIBT_INSTANCE=${INSTANCE}|" .env
    log "Instance name set to ${INSTANCE} (${BASE_INSTANCE} was in use)"
  fi
  log ".env created."
else
  log ".env already exists — skipping creation."
fi

AIBT_INSTANCE=$(get_env "AIBT_INSTANCE" "aibt")
AIBT_ROOT=$(get_env "AIBT_ROOT" "$SCRIPT_DIR")

# ── Step 2: Copy config template ─────────────────────────────────────────────
CONFIG_TEMPLATE="${1:-}"
COPY_CONFIG=false

if [[ -n "$CONFIG_TEMPLATE" ]]; then
  # Explicit template requested
  COPY_CONFIG=true
  if [[ ! -f "$CONFIG_TEMPLATE" ]]; then
    # Try with extension
    if [[ -f "${CONFIG_TEMPLATE}.json5.example" ]]; then
      CONFIG_TEMPLATE="${CONFIG_TEMPLATE}.json5.example"
    else
      echo "Error: config template not found: $CONFIG_TEMPLATE" >&2; exit 1
    fi
  fi
elif [[ ! -f config.json5 ]]; then
  COPY_CONFIG=true
  CONFIG_TEMPLATE="config.json5.example"
fi

if $COPY_CONFIG; then
  log "Copying ${CONFIG_TEMPLATE} → config.json5..."
  cp "$CONFIG_TEMPLATE" config.json5
fi

# ── Step 3: Create / update virtualenv ───────────────────────────────────────
if [[ ! -d venv ]]; then
  log "Creating virtualenv..."
  python3 -m venv venv
fi

log "Installing/updating Python dependencies..."
./venv/bin/pip install --upgrade pip -q
./venv/bin/pip install -r requirements.txt -q
log "Dependencies installed."

# ── Step 4: Create required directories ──────────────────────────────────────
mkdir -p logs sessions data/postgres

# ── Step 5: PostgreSQL (Docker) ───────────────────────────────────────────────
PG_CONTAINER="aibt-postgres-${AIBT_INSTANCE}"
PG_HOST=$(get_env "PG_HOST" "127.0.0.1")
PG_PORT=$(get_env "PG_PORT" "5432")
PG_USER=$(get_env "PG_USER" "aibt")
PG_PASSWORD=$(get_env "PG_PASSWORD" "changeme_aibt_db")
PG_DB=$(get_env "PG_DB" "aibt")

if command -v docker &>/dev/null && docker info &>/dev/null 2>&1; then
  if docker ps -a --format '{{.Names}}' | grep -qx "$PG_CONTAINER"; then
    STATE=$(docker inspect -f '{{.State.Running}}' "$PG_CONTAINER" 2>/dev/null || echo false)
    if [[ "$STATE" != "true" ]]; then
      log "Starting existing PostgreSQL container ${PG_CONTAINER}..."
      docker start "$PG_CONTAINER"
    else
      log "PostgreSQL container ${PG_CONTAINER} already running."
    fi
  else
    log "Pulling postgres:16-alpine..."
    docker pull postgres:16-alpine -q
    log "Creating PostgreSQL container ${PG_CONTAINER}..."
    docker run -d \
      --name "$PG_CONTAINER" \
      --restart unless-stopped \
      -e POSTGRES_USER="$PG_USER" \
      -e POSTGRES_PASSWORD="$PG_PASSWORD" \
      -e POSTGRES_DB="$PG_DB" \
      -p "${PG_HOST}:${PG_PORT}:5432" \
      -v "${AIBT_ROOT}/data/postgres:/var/lib/postgresql/data" \
      postgres:16-alpine
    log "PostgreSQL container started."
  fi
else
  log "WARNING: Docker not available — PostgreSQL container not started."
fi

# ── Step 6: Run doctor --fix (sets up systemd service and crontab) ────────────
log "Running doctor --fix..."
./venv/bin/python src/core/doctor.py --fix || true

# ── Summary ───────────────────────────────────────────────────────────────────
WEBUI_HOST=$(get_env "WEBUI_HOST" "0.0.0.0")
WEBUI_PORT=$(get_env "WEBUI_PORT" "50080")
DISPLAY_HOST="${WEBUI_HOST/0.0.0.0/localhost}"

echo
echo "───────────────────────────────────────────────"
echo " aibt installation complete"
echo "───────────────────────────────────────────────"
echo " Instance:  ${AIBT_INSTANCE}"
echo " Root:      ${AIBT_ROOT}"
echo " WebUI:     http://${DISPLAY_HOST}:${WEBUI_PORT}"
echo
echo " Key files:"
echo "   .env            — secrets and paths"
echo "   config.json5    — application config"
echo "   logs/           — application logs"
echo
echo " To start manually:    ./venv/bin/python src/core/app.py"
echo " To check status:      ./venv/bin/python src/core/doctor.py"
echo " To uninstall:         ./uninstall.sh"
echo "───────────────────────────────────────────────"
