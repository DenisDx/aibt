"""Periodic memoryd maintenance tasks.

Provides: cron tick runner for queue processing and retention enforcement.
"""

from __future__ import annotations

from typing import Any

from core.logging_utils import log
from memoryd.api import get_memoryd_service


def run_memoryd_cron(root_dir: str, config: dict[str, Any]) -> dict[str, Any]:
    """Run memoryd periodic tasks.

    Input: root path and app config.
    Output: status summary with counters.
    """

    log("memoryd", "debug", "call memoryd.cron.run_memoryd_cron")
    memoryd_cfg = config.get("memoryd", {}) if isinstance(config, dict) else {}
    svc = get_memoryd_service(root_dir, config)
    watchdog = svc.monitor_running_tasks()
    if not memoryd_cfg.get("enabled", False):
        # Do not skip pending tasks: they may come from envid-overlaid flows.
        try:
            pending = int(svc.store.count_pending_tasks())
        except Exception as e:
            log("memoryd", "warning", f"memoryd cron pending check failed while memoryd.enabled=false: {e}")
            return {"enabled": False, "queue": {"picked": 0, "started": 0, "done": 0, "failed": 0, "pruned": 0, "skipped": 0}, "watchdog": watchdog}
        if pending <= 0:
            log("memoryd", "info", "memoryd cron no-op: memoryd.enabled=false and pending queue is empty")
            return {"enabled": False, "queue": {"picked": 0, "started": 0, "done": 0, "failed": 0, "pruned": 0, "skipped": 0}, "watchdog": watchdog}
        log("memoryd", "info", f"memoryd cron processing pending queue while memoryd.enabled=false; pending={pending}")

    result = svc.run_tick(limit=int(memoryd_cfg.get("max_sim_task", 1)))
    if result.get("started") or result.get("done") or result.get("failed") or result.get("pruned"):
        log(
            "memoryd",
            "info",
            (
                "memoryd tick "
                f"picked={result.get('picked', 0)} started={result.get('started', 0)} "
                f"done={result.get('done', 0)} failed={result.get('failed', 0)} "
                f"pruned={result.get('pruned', 0)} skipped={result.get('skipped', 0)}"
            ),
        )
    return {"enabled": True, "queue": result, "watchdog": watchdog}
