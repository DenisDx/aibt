"""Periodic memory maintenance tasks."""

from __future__ import annotations

from typing import Any

from core.logging_utils import log
from memory.api import get_memory_service
from memory.langmem_manager import LangMemManager


def run_memory_cron(root_dir: str, config: dict[str, Any]) -> dict[str, Any]:
    """Run memory periodic tasks.

    Input: root path and app config.
    Output: status summary with ingest and consolidation counters.
    """

    memory_cfg = config.get("memory", {})
    if not memory_cfg.get("enabled", True):
        return {"enabled": False, "ingest": {"processed": 0, "failed": 0, "errors": []}}

    svc = get_memory_service(root_dir, config)
    ingest_cfg = memory_cfg.get("rag", {}).get("ingest", {})
    batch_limit = int(ingest_cfg.get("max_jobs_per_tick", 3))
    langmem = LangMemManager(root_dir, config)

    result = svc.run_ingest_batch(limit=max(1, batch_limit))
    if result.get("processed") or result.get("failed"):
        log(
            "memory",
            "info",
            f"ingest batch processed={result.get('processed', 0)} failed={result.get('failed', 0)}",
        )

    maintenance = langmem.run()
    if maintenance.get("summaries") or maintenance.get("semantic_promotions") or maintenance.get("archives"):
        log(
            "memory",
            "info",
            (
                "langmem maintenance "
                f"summaries={maintenance.get('summaries', 0)} "
                f"semantic_promotions={maintenance.get('semantic_promotions', 0)} "
                f"archives={maintenance.get('archives', 0)}"
            ),
        )

    return {"enabled": True, "ingest": result, "maintenance": maintenance}
