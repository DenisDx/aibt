#!/usr/bin/env bash
# Unified local test runner with readable progress report.
set -u
set -o pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

VENV_PY="${ROOT_DIR}/venv/bin/python"
WITH_SMOKE=0
FAIL_FAST=0

for arg in "$@"; do
  case "$arg" in
    --with-smoke) WITH_SMOKE=1 ;;
    --fail-fast) FAIL_FAST=1 ;;
    -h|--help)
      cat <<'EOF'
Usage: ./test.sh [--with-smoke] [--fail-fast]

Runs available project tests with a readable step-by-step report.

Options:
  --with-smoke   include smoke checks (may require running services and credentials)
  --fail-fast    stop on first failed mandatory step
  -h, --help     show this help
EOF
      exit 0
      ;;
    *)
      echo "Unknown option: $arg"
      exit 2
      ;;
  esac
done

if [[ ! -x "$VENV_PY" ]]; then
  echo "ERROR: Python venv interpreter not found: $VENV_PY"
  echo "Run installation first (./install.sh) or create venv manually."
  exit 2
fi

if [[ -t 1 ]]; then
  C_RESET='\033[0m'
  C_BOLD='\033[1m'
  C_BLUE='\033[34m'
  C_GREEN='\033[32m'
  C_YELLOW='\033[33m'
  C_RED='\033[31m'
else
  C_RESET=''
  C_BOLD=''
  C_BLUE=''
  C_GREEN=''
  C_YELLOW=''
  C_RED=''
fi

TOTAL_STEPS=0
PASSED_STEPS=0
FAILED_STEPS=0
SKIPPED_STEPS=0
FAILED_LABELS=()

TMP_DIR="${ROOT_DIR}/.test-results"
mkdir -p "$TMP_DIR"

line() {
  printf '%s\n' "--------------------------------------------------------------------------------"
}

ts_now() {
  date '+%H:%M:%S'
}

run_step() {
  local label="$1"
  local cmd="$2"
  local required="${3:-required}"

  TOTAL_STEPS=$((TOTAL_STEPS + 1))
  local log_file="${TMP_DIR}/step_${TOTAL_STEPS}.log"

  printf "%b[%s] STEP %d: %s%b\n" "$C_BLUE" "$(ts_now)" "$TOTAL_STEPS" "$label" "$C_RESET"
  printf "       command: %s\n" "$cmd"

  local start_ts
  start_ts="$(date +%s)"

  bash -lc "$cmd" >"$log_file" 2>&1
  local code=$?

  local end_ts
  end_ts="$(date +%s)"
  local elapsed=$((end_ts - start_ts))

  if [[ $code -eq 0 ]]; then
    PASSED_STEPS=$((PASSED_STEPS + 1))
    printf "       %bPASS%b (%ss)\n" "$C_GREEN" "$C_RESET" "$elapsed"
    local out_lines
    out_lines=$(wc -l <"$log_file" | tr -d ' ')
    if [[ "$out_lines" -gt 0 ]]; then
      printf "       output (tail):\n"
      tail -n 8 "$log_file" | sed 's/^/         /'
    fi
    return 0
  fi

  if [[ "$required" == "optional" ]]; then
    SKIPPED_STEPS=$((SKIPPED_STEPS + 1))
    printf "       %bSKIP%b (%ss) optional step failed with code %d\n" "$C_YELLOW" "$C_RESET" "$elapsed" "$code"
    printf "       output (tail):\n"
    tail -n 12 "$log_file" | sed 's/^/         /'
    return 0
  fi

  FAILED_STEPS=$((FAILED_STEPS + 1))
  FAILED_LABELS+=("$label")
  printf "       %bFAIL%b (%ss) exit code %d\n" "$C_RED" "$C_RESET" "$elapsed" "$code"
  printf "       output (tail):\n"
  tail -n 20 "$log_file" | sed 's/^/         /'

  if [[ $FAIL_FAST -eq 1 ]]; then
    printf "%bStopping due to --fail-fast.%b\n" "$C_RED" "$C_RESET"
    summary
    exit 1
  fi
  return 1
}

summary() {
  line
  printf "%bTest Summary%b\n" "$C_BOLD" "$C_RESET"
  printf "  total   : %d\n" "$TOTAL_STEPS"
  printf "  passed  : %d\n" "$PASSED_STEPS"
  printf "  failed  : %d\n" "$FAILED_STEPS"
  printf "  skipped : %d\n" "$SKIPPED_STEPS"

  if [[ ${#FAILED_LABELS[@]} -gt 0 ]]; then
    printf "  failed steps:\n"
    local item
    for item in "${FAILED_LABELS[@]}"; do
      printf "    - %s\n" "$item"
    done
  fi

  if [[ $FAILED_STEPS -eq 0 ]]; then
    printf "%bRESULT: SUCCESS%b\n" "$C_GREEN" "$C_RESET"
  else
    printf "%bRESULT: FAILED%b\n" "$C_RED" "$C_RESET"
  fi
}

line
printf "%bAIBT Test Runner%b\n" "$C_BOLD" "$C_RESET"
printf "root: %s\n" "$ROOT_DIR"
printf "python: %s\n" "$VENV_PY"
printf "with smoke: %s\n" "$([[ $WITH_SMOKE -eq 1 ]] && echo yes || echo no)"
printf "fail fast: %s\n" "$([[ $FAIL_FAST -eq 1 ]] && echo yes || echo no)"
line

# 1) Python syntax checks (broad, fast)
run_step "Python syntax check (src, webui backend, tests)" \
  "cd '$ROOT_DIR' && find src webui/backend tests -type f -name '*.py' -print0 | xargs -0 '$VENV_PY' -m py_compile"

# 2) Frontend syntax check when Node is available
if command -v node >/dev/null 2>&1; then
  run_step "Frontend syntax check (webui/frontend/app.js)" \
    "cd '$ROOT_DIR' && node --check webui/frontend/app.js"
else
  TOTAL_STEPS=$((TOTAL_STEPS + 1))
  SKIPPED_STEPS=$((SKIPPED_STEPS + 1))
  printf "%b[%s] STEP %d: Frontend syntax check%b\n" "$C_BLUE" "$(ts_now)" "$TOTAL_STEPS" "$C_RESET"
  printf "       %bSKIP%b node is not installed\n" "$C_YELLOW" "$C_RESET"
fi

# 3) Unit tests from tests/
if [[ -d "$ROOT_DIR/tests" ]]; then
  run_step "Python unit tests (unittest discover in tests/)" \
    "cd '$ROOT_DIR' && '$VENV_PY' -m unittest discover -s tests -p 'test*.py'"
else
  TOTAL_STEPS=$((TOTAL_STEPS + 1))
  SKIPPED_STEPS=$((SKIPPED_STEPS + 1))
  printf "%b[%s] STEP %d: Python unit tests%b\n" "$C_BLUE" "$(ts_now)" "$TOTAL_STEPS" "$C_RESET"
  printf "       %bSKIP%b tests/ directory not found\n" "$C_YELLOW" "$C_RESET"
fi

# 4) Optional smoke tests (opt-in)
if [[ $WITH_SMOKE -eq 1 ]]; then
  if [[ -f "$ROOT_DIR/scripts/memoryd_smoke.py" ]]; then
    run_step "Smoke test (memoryd_smoke.py)" \
      "cd '$ROOT_DIR' && '$VENV_PY' scripts/memoryd_smoke.py --envid envid-telegram-bot2-memoryd --muid smoke" \
      "optional"
  else
    TOTAL_STEPS=$((TOTAL_STEPS + 1))
    SKIPPED_STEPS=$((SKIPPED_STEPS + 1))
    printf "%b[%s] STEP %d: Smoke test (memoryd_smoke.py)%b\n" "$C_BLUE" "$(ts_now)" "$TOTAL_STEPS" "$C_RESET"
    printf "       %bSKIP%b scripts/memoryd_smoke.py not found\n" "$C_YELLOW" "$C_RESET"
  fi
fi

summary
[[ $FAILED_STEPS -eq 0 ]] && exit 0 || exit 1
