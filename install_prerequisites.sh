#!/usr/bin/env bash
# install_prerequisites.sh — Check and install system-level dependencies.
# Idempotent: safe to run multiple times.
set -euo pipefail

OK=0; FIXED=0; FAIL=0
pass()  { echo "  [OK  ] $*"; ((OK++));    }
fixed() { echo "  [DONE] $*"; ((FIXED++)); }
fail()  { echo "  [FAIL] $*"; ((FAIL++));  }

need_sudo() {
  if [[ $EUID -eq 0 ]]; then "$@"; else sudo "$@"; fi
}

# ── Python 3.11+ ─────────────────────────────────────────────────────────────
check_python() {
  local py
  for py in python3.12 python3.11 python3; do
    if command -v "$py" &>/dev/null; then
      local ver
      ver=$("$py" -c "import sys; print(sys.version_info[:2])" 2>/dev/null)
      if "$py" -c "import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)" 2>/dev/null; then
        pass "Python $ver found at $(command -v "$py")"
        return 0
      fi
    fi
  done

  echo "  Python 3.11+ not found — installing..."
  if command -v apt-get &>/dev/null; then
    need_sudo apt-get update -qq
    need_sudo apt-get install -y python3 python3-dev
    fixed "python3 installed"
  else
    fail "Cannot install Python automatically. Please install Python 3.11+ manually."
  fi
}

# ── python3-venv ──────────────────────────────────────────────────────────────
check_venv() {
  if python3 -c "import venv" 2>/dev/null; then
    pass "python3-venv available"
    return 0
  fi
  echo "  python3-venv missing — installing..."
  if command -v apt-get &>/dev/null; then
    need_sudo apt-get install -y python3-venv python3-pip
    fixed "python3-venv installed"
  else
    fail "Cannot install python3-venv automatically. Please install it manually."
  fi
}

# ── Docker ────────────────────────────────────────────────────────────────────
check_docker() {
  if command -v docker &>/dev/null && docker info &>/dev/null 2>&1; then
    pass "Docker available ($(docker --version | head -1))"
    return 0
  fi
  if command -v docker &>/dev/null; then
    fail "Docker installed but daemon not running or insufficient permissions."
    echo "     Run: sudo usermod -aG docker \$USER  then log out and back in."
    return 0
  fi

  echo "  Docker not found — installing..."
  if command -v apt-get &>/dev/null; then
    need_sudo apt-get update -qq
    need_sudo apt-get install -y ca-certificates curl gnupg lsb-release
    need_sudo install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
      | need_sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
    need_sudo chmod a+r /etc/apt/keyrings/docker.gpg
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" \
      | need_sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
    need_sudo apt-get update -qq
    need_sudo apt-get install -y docker-ce docker-ce-cli containerd.io
    need_sudo systemctl enable --now docker
    need_sudo usermod -aG docker "$USER" || true
    fixed "Docker installed. You may need to log out and back in for group membership to take effect."
  else
    fail "Cannot install Docker automatically. Please install Docker manually: https://docs.docker.com/get-docker/"
  fi
}

# ── Run all checks ────────────────────────────────────────────────────────────
echo "aibt prerequisite check"
echo
check_python
check_venv
check_docker
echo
echo "Results: ${OK} ok, ${FIXED} installed, ${FAIL} failed."
if [[ $FAIL -gt 0 ]]; then
  echo "Please resolve the failures above before running install.sh."
  exit 1
fi
echo "All prerequisites satisfied."
