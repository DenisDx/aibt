#!/usr/bin/env python3
"""System health checker and fixer.

Usage:
  python doctor.py          # report only
  python doctor.py --fix    # report and apply fixes
"""
from __future__ import annotations
import sys
import os
import subprocess
import argparse
from collections import Counter

_ROOT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_SRC_DIR = os.path.join(_ROOT_DIR, "src")
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)


class Check:
    """Result of a single health check."""

    def __init__(self, name: str, ok: bool, message: str, fixed: bool = False) -> None:
        self.name = name
        self.ok = ok
        self.message = message
        self.fixed = fixed

    def __str__(self) -> str:
        if self.ok and self.fixed:
            status = "FIXED"
        elif self.ok:
            status = "OK   "
        else:
            status = "FAIL "
        return f"  [{status}] {self.name}: {self.message}"


# ── Individual checks ────────────────────────────────────────────────────────

def chk_env(fix: bool) -> Check:
    """.env file exists."""
    path = os.path.join(_ROOT_DIR, ".env")
    if os.path.exists(path):
        return Check(".env", True, "exists")
    return Check(".env", False, ".env missing — run install.sh")


def chk_config(fix: bool) -> Check:
    """config.json5 parses without errors (syntax check only for phase 1)."""
    try:
        from core.config import load_config
        cfg = load_config(_ROOT_DIR)
        return Check("config.json5", True, f"valid (title: {cfg.get('title', '?')})")
    except FileNotFoundError:
        return Check("config.json5", False, "file not found — run install.sh")
    except Exception as e:
        return Check("config.json5", False, f"parse error: {e}")


def chk_envids(fix: bool) -> Check:
    """Validate envid definitions and references.

    Input: fix mode flag (unused).
    Output: health check status for envid configuration.
    """

    del fix
    try:
        from core.config import load_config

        cfg = load_config(_ROOT_DIR)
    except Exception as e:
        return Check("envids", False, f"cannot load config: {e}")

    envids_cfg = cfg.get("envids", {}) if isinstance(cfg, dict) else {}
    if not isinstance(envids_cfg, dict) or not bool(envids_cfg.get("enabled", False)):
        return Check("envids", True, "disabled")

    items = envids_cfg.get("items", {}) if isinstance(envids_cfg, dict) else {}
    if not isinstance(items, dict) or not items:
        return Check("envids", False, "enabled but envids.items is empty or invalid")

    errors: list[str] = []

    startup_order = envids_cfg.get("startup_order", [])
    if isinstance(startup_order, list):
        order = [str(x).strip() for x in startup_order if str(x).strip()]
        missing = [x for x in order if x not in items]
        if missing:
            errors.append(f"startup_order references unknown envid: {', '.join(missing[:3])}")
        if len(order) != len(set(order)):
            errors.append("startup_order contains duplicates")

    adapter_order = envids_cfg.get("adapter_startup_order", {})
    if isinstance(adapter_order, dict):
        for adapter_name, order_raw in adapter_order.items():
            if not isinstance(order_raw, list):
                errors.append(f"adapter_startup_order.{adapter_name} must be list")
                continue
            order = [str(x).strip() for x in order_raw if str(x).strip()]
            missing = [x for x in order if x not in items]
            if missing:
                errors.append(f"adapter_startup_order.{adapter_name} unknown envid: {', '.join(missing[:3])}")

    declared_agent_ids = set()
    agents_items = cfg.get("agents", {}).get("items", {}) if isinstance(cfg.get("agents", {}), dict) else {}
    if isinstance(agents_items, dict):
        declared_agent_ids.update(str(x).strip() for x in agents_items.keys() if str(x).strip())

    agents_dir = os.path.join(_ROOT_DIR, "src", "agents")
    if os.path.isdir(agents_dir):
        for entry in os.listdir(agents_dir):
            if os.path.isdir(os.path.join(agents_dir, entry)):
                declared_agent_ids.add(entry)

    telegram_chat_ids: list[str] = []
    for envid, entry in items.items():
        env_key = str(envid).strip()
        if not env_key:
            errors.append("found empty envid key")
            continue
        if not isinstance(entry, dict):
            errors.append(f"envid '{env_key}' must be object")
            continue

        matching = entry.get("matching", {})
        if not isinstance(matching, dict):
            errors.append(f"envid '{env_key}' matching must be object")
            matching = {}
        adapters = matching.get("adapters", {}) if isinstance(matching, dict) else {}
        if adapters and not isinstance(adapters, dict):
            errors.append(f"envid '{env_key}' matching.adapters must be object")
            adapters = {}

        tg = adapters.get("telegram", {}) if isinstance(adapters, dict) else {}
        if isinstance(tg, dict):
            chat_ids = tg.get("chat_ids", [])
            if isinstance(chat_ids, list):
                telegram_chat_ids.extend(str(x).strip() for x in chat_ids if str(x).strip())

        overlay = entry.get("config", {}) if isinstance(entry, dict) else {}
        if not isinstance(overlay, dict):
            errors.append(f"envid '{env_key}' config must be object")
            continue

        env_agents = overlay.get("agents", {}).get("items", {}) if isinstance(overlay.get("agents", {}), dict) else {}
        if isinstance(env_agents, dict):
            for agent_id, agent_cfg in env_agents.items():
                clean_agent = str(agent_id).strip()
                if not clean_agent:
                    continue
                if clean_agent not in declared_agent_ids:
                    errors.append(f"envid '{env_key}' references unknown agent '{clean_agent}'")
                if not isinstance(agent_cfg, dict):
                    continue

                agentmd_file = str(agent_cfg.get("agentmd_file", "")).strip()
                if agentmd_file and not os.path.exists(agentmd_file):
                    errors.append(f"envid '{env_key}' missing agentmd_file: {agentmd_file}")

                instruction_files = agent_cfg.get("instruction_files")
                if isinstance(instruction_files, list):
                    for file_path in instruction_files:
                        path = str(file_path).strip()
                        if path and not os.path.exists(path):
                            errors.append(f"envid '{env_key}' missing instruction file: {path}")

        env_adapters = overlay.get("adapters", {}).get("items", {}) if isinstance(overlay.get("adapters", {}), dict) else {}
        tg_cfg = env_adapters.get("telegram", {}) if isinstance(env_adapters, dict) else {}
        if isinstance(tg_cfg, dict):
            default_agent = str(tg_cfg.get("default_agent", "")).strip()
            if default_agent and default_agent not in declared_agent_ids:
                errors.append(f"envid '{env_key}' telegram.default_agent unknown: {default_agent}")

    id_counts = Counter(x for x in telegram_chat_ids if x)
    duplicates = [chat_id for chat_id, cnt in id_counts.items() if cnt > 1]
    if duplicates:
        errors.append(f"ambiguous telegram chat_ids across envids: {', '.join(duplicates[:3])}")

    if errors:
        return Check("envids", False, "; ".join(errors[:5]))
    return Check("envids", True, f"valid ({len(items)} environments)")


def chk_venv(fix: bool) -> Check:
    """venv exists with a Python interpreter."""
    py = os.path.join(_ROOT_DIR, "venv", "bin", "python")
    if os.path.exists(py):
        return Check("venv", True, f"found at venv/bin/python")
    return Check("venv", False, "venv not found — run install.sh")


def chk_logs_dir(fix: bool) -> Check:
    """logs/ directory exists."""
    d = os.path.join(_ROOT_DIR, "logs")
    if os.path.isdir(d):
        return Check("logs/", True, "exists")
    if fix:
        os.makedirs(d, exist_ok=True)
        return Check("logs/", True, "created", fixed=True)
    return Check("logs/", False, "missing (will be created on --fix)")


def _instance_name() -> str:
    try:
        from core.config import load_env_file
        return load_env_file(_ROOT_DIR).get("AIBT_INSTANCE", "aibt")
    except Exception:
        return "aibt"


def chk_crontab(fix: bool) -> Check:
    """cron.py is registered in user crontab."""
    venv_py = os.path.join(_ROOT_DIR, "venv", "bin", "python")
    cron_py = os.path.join(_ROOT_DIR, "src", "core", "cron.py")
    cron_log = os.path.join(_ROOT_DIR, "logs", "cron-output.log")

    try:
        r = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
        current = r.stdout if r.returncode == 0 else ""
    except Exception as e:
        return Check("crontab", False, f"crontab unavailable: {e}")

    if cron_py in current:
        return Check("crontab", True, "cron.py scheduled")
    if not fix:
        return Check("crontab", False, "cron.py not in crontab (run with --fix)")

    entry = f"* * * * * {venv_py} {cron_py} >> {cron_log} 2>&1\n"
    new_tab = (current.rstrip("\n") + "\n" if current.strip() else "") + entry
    try:
        r2 = subprocess.run(["crontab", "-"], input=new_tab, text=True, capture_output=True)
        if r2.returncode == 0:
            return Check("crontab", True, "cron.py added", fixed=True)
        return Check("crontab", False, f"crontab update failed: {r2.stderr.strip()}")
    except Exception as e:
        return Check("crontab", False, f"Failed: {e}")


def chk_service(fix: bool) -> Check:
    """systemd service is active."""
    instance = _instance_name()
    svc = instance
    is_root = os.geteuid() == 0
    sd = [] if is_root else ["--user"]

    r = subprocess.run(["systemctl"] + sd + ["is-active", svc], capture_output=True, text=True)
    if r.stdout.strip() == "active":
        return Check("service", True, f"{svc} is active")
    if not fix:
        return Check("service", False, f"{svc} not active (run with --fix)")

    # Build service unit
    venv_py = os.path.join(_ROOT_DIR, "venv", "bin", "python")
    app_py = os.path.join(_ROOT_DIR, "src", "core", "app.py")
    wants = "multi-user.target" if is_root else "default.target"
    unit = (
        f"[Unit]\nDescription=aibt - Multi-agent AI platform ({instance})\nAfter=network.target\n\n"
        f"[Service]\nType=simple\nWorkingDirectory={_ROOT_DIR}\n"
        f"ExecStart={venv_py} {app_py}\nRestart=always\nRestartSec=5\n"
        f"Environment=PYTHONUNBUFFERED=1\n\n"
        f"[Install]\nWantedBy={wants}\n"
    )
    if is_root:
        svc_path = f"/etc/systemd/system/{svc}.service"
    else:
        d = os.path.expanduser("~/.config/systemd/user")
        os.makedirs(d, exist_ok=True)
        svc_path = os.path.join(d, f"{svc}.service")

    try:
        with open(svc_path, "w") as f:
            f.write(unit)
        subprocess.run(["systemctl"] + sd + ["daemon-reload"], check=True, capture_output=True)
        subprocess.run(["systemctl"] + sd + ["enable", "--now", svc], check=True, capture_output=True)
        return Check("service", True, f"{svc} created and started", fixed=True)
    except Exception as e:
        return Check("service", False, f"Failed to create {svc}: {e}")


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description="aibt health checker")
    parser.add_argument("--fix", action="store_true", help="Apply fixes where possible")
    args = parser.parse_args()

    print(f"aibt Doctor {'[fix mode]' if args.fix else '[report only]'}")
    print(f"Root: {_ROOT_DIR}\n")

    checks = [
        chk_env(args.fix),
        chk_config(args.fix),
        chk_envids(args.fix),
        chk_venv(args.fix),
        chk_logs_dir(args.fix),
        chk_crontab(args.fix),
        chk_service(args.fix),
    ]

    for c in checks:
        print(c)

    failed = [c for c in checks if not c.ok]
    print()
    if not failed:
        print("All checks passed.")
        return 0
    hint = "" if args.fix else " Run with --fix to attempt repairs."
    print(f"{len(failed)} check(s) failed.{hint}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
