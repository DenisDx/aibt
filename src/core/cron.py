#!/usr/bin/env python3
"""Periodic task runner. Invoked by crontab; runs within the full app environment."""
from __future__ import annotations
import sys
import os
import fcntl
import re
import time

_ROOT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_SRC_DIR = os.path.join(_ROOT_DIR, "src")
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from core.config import load_config
from core.logging_utils import init_logging, log
from memory.cron_tasks import run_memory_cron
from memoryd import run_memoryd_cron


# Timestamp pattern embedded in log lines: ]YYYY-MM-DD HH:MM:SS.mmm
_TS_RE = re.compile(r'\](\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3})')


def _acquire_lock(root_dir: str) -> int | None:
    """Acquire exclusive lock file to prevent overlapping cron runs. Returns fd or None."""
    lock_path = os.path.join(root_dir, "logs", ".cron.lock")
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except (OSError, IOError):
        return None


def _release_lock(fd: int) -> None:
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    except Exception:
        pass


def _wipe_logs(config: dict, root_dir: str) -> None:
    """Remove log lines exceeding wipe_max_age or causing file to exceed max_log_size.

    config:   app config dict
    root_dir: project root directory
    """
    logging_cfg = config.get("logging", {})
    max_age = int(logging_cfg.get("wipe_max_age", 0))
    max_size = int(logging_cfg.get("max_log_size", 0))
    if not max_age and not max_size:
        return

    logs_dir = os.path.join(root_dir, "logs")
    if not os.path.isdir(logs_dir):
        return

    now = time.time()
    for fname in os.listdir(logs_dir):
        if not fname.endswith(".log") or fname.startswith("."):
            continue
        fpath = os.path.join(logs_dir, fname)
        try:
            with open(fpath, encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
        except Exception:
            continue
        if not lines:
            continue

        if max_age:
            kept = []
            for line in lines:
                m = _TS_RE.search(line)
                if m:
                    try:
                        from datetime import datetime as _dt
                        ts = _dt.strptime(m.group(1), "%Y-%m-%d %H:%M:%S.%f").timestamp()
                        if now - ts <= max_age:
                            kept.append(line)
                        # else: too old, drop
                    except Exception:
                        kept.append(line)
                else:
                    kept.append(line)
            lines = kept

        if max_size:
            total = sum(len(l.encode("utf-8")) for l in lines)
            while lines and total > max_size:
                total -= len(lines[0].encode("utf-8"))
                lines.pop(0)

        try:
            with open(fpath, "w", encoding="utf-8") as f:
                f.writelines(lines)
        except Exception as e:
            log("cron", "error", f"Failed to wipe {fname}: {e}")


def _should_wipe(config: dict, root_dir: str) -> bool:
    """Return True if wipe_period has elapsed since last wipe."""
    period = int(config.get("logging", {}).get("wipe_period", 0))
    if not period:
        return False
    state = os.path.join(root_dir, "logs", ".last_wipe")
    try:
        last = float(open(state).read().strip())
        return time.time() - last >= period
    except Exception:
        return True


def _mark_wiped(root_dir: str) -> None:
    """Record current time as last wipe timestamp."""
    state = os.path.join(root_dir, "logs", ".last_wipe")
    try:
        with open(state, "w") as f:
            f.write(str(time.time()))
    except Exception:
        pass


def main() -> None:
    """Run all periodic tasks. Exits silently if another instance is already running."""
    fd = _acquire_lock(_ROOT_DIR)
    if fd is None:
        return  # another cron instance is running

    try:
        config = load_config(_ROOT_DIR)
        init_logging(config, _ROOT_DIR)
        log("cron", "debug", "Cron tick")

        if _should_wipe(config, _ROOT_DIR):
            log("cron", "info", "Starting log wipe")
            _wipe_logs(config, _ROOT_DIR)
            _mark_wiped(_ROOT_DIR)
            log("cron", "info", "Log wipe completed")

        try:
            memory_result = run_memory_cron(_ROOT_DIR, config)
            if memory_result.get("enabled"):
                ingest = memory_result.get("ingest", {})
                if ingest.get("processed") or ingest.get("failed"):
                    log(
                        "cron",
                        "info",
                        f"Memory cron done: processed={ingest.get('processed', 0)} failed={ingest.get('failed', 0)}",
                    )
        except Exception as e:
            log("cron", "error", f"Memory cron error: {e}")

        try:
            memoryd_result = run_memoryd_cron(_ROOT_DIR, config)
            if memoryd_result.get("enabled"):
                queue = memoryd_result.get("queue", {})
                if queue.get("started") or queue.get("done") or queue.get("failed") or queue.get("pruned"):
                    log(
                        "cron",
                        "info",
                        (
                            "Memoryd cron done: "
                            f"picked={queue.get('picked', 0)} started={queue.get('started', 0)} "
                            f"done={queue.get('done', 0)} failed={queue.get('failed', 0)} "
                            f"pruned={queue.get('pruned', 0)} skipped={queue.get('skipped', 0)}"
                        ),
                    )
        except Exception as e:
            log("cron", "error", f"Memoryd cron error: {e}")

    except Exception as e:
        log("cron", "error", f"Cron error: {e}")
    finally:
        _release_lock(fd)


if __name__ == "__main__":
    main()
