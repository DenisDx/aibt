"""Centralized logging subsystem.

Public API:
  init_logging(config, root_dir)   -- initialize before first log() call
  log(type, level, message, tag)   -- write log entry
  register_log_listener(queue)     -- subscribe asyncio.Queue to live log feed
  unregister_log_listener(queue)   -- unsubscribe
"""
from __future__ import annotations
import os
import threading
from datetime import datetime
from typing import Any

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None  # type: ignore

# 0=emergency .. 7=debug  (systemd-journald scale)
_LEVEL_NAMES: dict[int, str] = {
    0: "EMERGENCY", 1: "ALERT", 2: "CRITICAL", 3: "ERROR",
    4: "WARNING", 5: "NOTICE", 6: "INFO", 7: "DEBUG",
}
_NAME_TO_INT: dict[str, int] = {v.lower(): k for k, v in _LEVEL_NAMES.items()}

_config: dict = {}
_root_dir: str = ""
_file_lock = threading.Lock()
_ws_lock = threading.Lock()
_ws_queues: list[Any] = []  # active asyncio.Queue instances


def init_logging(config: dict, root_dir: str) -> None:
    """Initialize logging. Call early; safe to call multiple times (re-init)."""
    global _config, _root_dir
    _config = config
    _root_dir = root_dir
    if root_dir:
        os.makedirs(os.path.join(root_dir, "logs"), exist_ok=True)


def _resolve_level(level: int | str) -> int:
    """Convert level value to int 0-7."""
    if isinstance(level, int):
        return max(0, min(7, level))
    return _NAME_TO_INT.get(str(level).lower(), 6)


def _threshold(log_type: str) -> int:
    """Return effective log level threshold for the given subsystem type."""
    logging_cfg = _config.get("logging", {})
    default = _resolve_level(logging_cfg.get("level", 6))
    levels = logging_cfg.get("levels", {})
    return _resolve_level(levels[log_type]) if log_type in levels else default


def _timestamp() -> str:
    """Return formatted timestamp with timezone using configured timezone."""
    tz_name = _config.get("logging", {}).get("timezone", "local") if _config else "local"
    try:
        if tz_name == "local":
            now = datetime.now().astimezone()
        elif ZoneInfo is not None:
            now = datetime.now(ZoneInfo(tz_name))
        else:
            now = datetime.now().astimezone()
    except Exception:
        now = datetime.now().astimezone()
    ms = f"{now.microsecond // 1000:03d}"
    return now.strftime(f"%Y-%m-%d %H:%M:%S.{ms}%z")


def register_log_listener(queue: Any) -> None:
    """Register asyncio.Queue to receive (log_type, line) tuples for live streaming."""
    with _ws_lock:
        _ws_queues.append(queue)


def unregister_log_listener(queue: Any) -> None:
    """Unregister a previously registered queue."""
    with _ws_lock:
        try:
            _ws_queues.remove(queue)
        except ValueError:
            pass


def log(type: str, level: int | str, message: str, tag: str | None = None) -> None:
    """Write log entry to logs/all.log and logs/<type>.log.

    type:    subsystem name (core, agent, webui, cron, ...)
    level:   0-7 int or string (emergency/alert/critical/error/warning/notice/info/debug)
    message: log text
    tag:     optional sub-identifier (e.g. agent name)
    """
    level_int = _resolve_level(level)
    if _config and level_int > _threshold(type):
        return

    tag_part = f"[{tag}]" if tag else ""
    line = f"[{type.upper()}]{tag_part}[{_LEVEL_NAMES.get(level_int, str(level_int))}]{_timestamp()}: {message}\n"

    logs_dir = os.path.join(_root_dir, "logs") if _root_dir else "logs"
    with _file_lock:
        for fname in ("all.log", f"{type.lower()}.log"):
            try:
                with open(os.path.join(logs_dir, fname), "a", encoding="utf-8") as f:
                    f.write(line)
            except Exception:
                pass

    # Broadcast to WebSocket listeners (non-blocking, thread-safe)
    with _ws_lock:
        queues = list(_ws_queues)
    for q in queues:
        try:
            q.put_nowait((type.lower(), line.rstrip()))
        except Exception:
            pass
