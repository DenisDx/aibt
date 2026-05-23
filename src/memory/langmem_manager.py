"""Working-memory maintenance policies for agent memory."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
import os
from typing import Any

from memory import get_memory_service


class LangMemManager:
    """Run lightweight consolidation and summarization over agent memory.

    Input: app config and root dir.
    Output: batch maintenance summaries and write-backs.
    """

    def __init__(self, root_dir: str, config: dict[str, Any]):
        self.root_dir = root_dir
        self.config = config
        self.memory_cfg = config.get("memory", {})
        self.langmem_cfg = self.memory_cfg.get("langmem", {}) if isinstance(self.memory_cfg, dict) else {}
        self.service = get_memory_service(root_dir, config)
        self.agent_root = os.path.join(self.service.data_root, "agent")

    def run(self) -> dict[str, Any]:
        """Run maintenance for all known agents.

        Input: none.
        Output: aggregated maintenance counters.
        """

        if not self.memory_cfg.get("enabled", True):
            return {"enabled": False, "agents": [], "summaries": 0, "semantic_promotions": 0, "archives": 0}

        agent_ids = self._agent_ids()
        summary_count = 0
        semantic_count = 0
        archive_count = 0
        agent_reports: list[dict[str, Any]] = []

        for agent_id in agent_ids:
            report = self.run_for_agent(agent_id)
            agent_reports.append(report)
            summary_count += int(report.get("summaries", 0))
            semantic_count += int(report.get("semantic_promotions", 0))
            archive_count += int(report.get("archives", 0))

        return {
            "enabled": True,
            "agents": agent_reports,
            "summaries": summary_count,
            "semantic_promotions": semantic_count,
            "archives": archive_count,
        }

    def run_for_agent(self, agent_id: str) -> dict[str, Any]:
        """Run maintenance for one agent namespace.

        Input: agent id.
        Output: per-agent maintenance counters.
        """

        clean_agent = str(agent_id or "").strip()
        if not clean_agent:
            return {"agent_id": "", "summaries": 0, "semantic_promotions": 0, "archives": 0}

        summary_count = self._summarize_threads(clean_agent)
        semantic_count = self._promote_semantic_facts(clean_agent)
        archive_count = self._archive_old_episodes(clean_agent)
        return {
            "agent_id": clean_agent,
            "summaries": summary_count,
            "semantic_promotions": semantic_count,
            "archives": archive_count,
        }

    def extract_semantic_facts(self, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Extract durable semantic facts from episodic events.

        Input: episodic events.
        Output: extracted facts list.
        """

        facts: list[dict[str, Any]] = []
        for event in events:
            text = str(event.get("text") or "").strip()
            if not text:
                continue
            importance = float(event.get("importance") or 0.0)
            if importance < self._min_importance():
                importance = self._heuristic_importance(text, event)
            if importance < self._min_importance():
                continue
            facts.append(
                {
                    "text": self._normalize_text(text),
                    "scope": event.get("scope") or event.get("thread_id") or event.get("task_id"),
                    "importance": round(max(importance, 0.0), 3),
                    "source": {
                        "ts": event.get("ts"),
                        "task_id": event.get("task_id"),
                        "outcome": event.get("outcome"),
                    },
                }
            )
        return self._dedupe_records(facts)

    def _summarize_threads(self, agent_id: str) -> int:
        """Write compact session summaries for stale episodic threads.

        Input: agent id.
        Output: number of summary records written.
        """

        episodes = self._read_namespace(agent_id, "episodic", limit=500)
        if not episodes:
            return 0

        existing = self._read_namespace(agent_id, "summaries", limit=500)
        existing_by_thread = {str(item.get("thread_id") or item.get("task_id") or ""): item for item in existing}

        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for item in episodes:
            thread_id = str(item.get("task_id") or item.get("thread_id") or "unthreaded")
            groups[thread_id].append(item)

        cutoff_hours = int(self.langmem_cfg.get("summary_inactive_after_hours", 24))
        cutoff_seconds = max(0, cutoff_hours) * 3600
        now = datetime.now(timezone.utc)
        written = 0

        for thread_id, items in groups.items():
            latest_ts = self._parse_ts(items[-1].get("ts"))
            if latest_ts and cutoff_seconds and (now - latest_ts).total_seconds() < cutoff_seconds:
                continue

            existing_summary = existing_by_thread.get(thread_id)
            latest_episode_ts = str(items[-1].get("ts") or "")
            if existing_summary:
                if str(existing_summary.get("latest_episode_ts") or "") == latest_episode_ts and int(existing_summary.get("episode_count") or 0) == len(items):
                    continue

            summary_text = self._build_thread_summary(items)
            payload = {
                "ts": now.isoformat(),
                "thread_id": thread_id,
                "task_id": thread_id if thread_id != "unthreaded" else None,
                "text": summary_text,
                "episode_count": len(items),
                "latest_episode_ts": latest_episode_ts,
                "source": "langmem",
            }
            self._append_namespace(agent_id, "summaries", payload)
            written += 1

        return written

    def _promote_semantic_facts(self, agent_id: str) -> int:
        """Promote strong episodic signals into semantic memory.

        Input: agent id.
        Output: number of semantic records written.
        """

        events = self._read_namespace(agent_id, "episodic", limit=500)
        if not events:
            return 0

        candidates = self.extract_semantic_facts(events)
        existing = self._read_namespace(agent_id, "semantic", limit=1000)
        existing_keys = {self._normalize_text(str(item.get("text") or "")) for item in existing}

        written = 0
        max_per_tick = int(self.langmem_cfg.get("max_semantic_promotions_per_tick", 8))
        for fact in candidates:
            normalized = self._normalize_text(str(fact.get("text") or ""))
            if not normalized or normalized in existing_keys:
                continue
            self._append_namespace(agent_id, "semantic", {
                "ts": datetime.now(timezone.utc).isoformat(),
                "text": fact.get("text"),
                "scope": fact.get("scope"),
                "importance": fact.get("importance", 0.0),
                "source": fact.get("source", {}),
                "promoted_by": "langmem",
            })
            existing_keys.add(normalized)
            written += 1
            if written >= max_per_tick:
                break

        return written

    def _archive_old_episodes(self, agent_id: str) -> int:
        """Summarize old episodic items into archival summaries.

        Input: agent id.
        Output: number of archived summary records written.
        """

        episodes = self._read_namespace(agent_id, "episodic", limit=1000)
        if not episodes:
            return 0

        retain_recent = int(self.langmem_cfg.get("retain_recent_episodes", 150))
        archive_after_hours = int(self.langmem_cfg.get("archive_after_hours", 72))
        cutoff_seconds = max(0, archive_after_hours) * 3600
        if len(episodes) <= retain_recent:
            return 0

        now = datetime.now(timezone.utc)
        archived = 0
        for item in episodes[:-retain_recent]:
            ts = self._parse_ts(item.get("ts"))
            if ts and cutoff_seconds and (now - ts).total_seconds() < cutoff_seconds:
                continue
            text = str(item.get("text") or "").strip()
            if not text:
                continue
            self._append_namespace(agent_id, "summaries", {
                "ts": now.isoformat(),
                "thread_id": str(item.get("task_id") or item.get("thread_id") or "unthreaded"),
                "task_id": item.get("task_id"),
                "text": f"Archived episode: {text[:280]}",
                "source": "langmem-archive",
            })
            archived += 1
            if archived >= int(self.langmem_cfg.get("max_archives_per_tick", 8)):
                break

        return archived

    def _read_namespace(self, agent_id: str, namespace: str, limit: int) -> list[dict[str, Any]]:
        """Read latest namespace records.

        Input: agent id, namespace, and record cap.
        Output: list of records.
        """

        return self.service.read_namespace_items(self.service._namespace_tuple(agent_id, namespace), limit=limit)

    @staticmethod
    def _coerce_record(raw: str) -> dict[str, Any]:
        """Parse one JSONL record.

        Input: raw JSON line.
        Output: parsed dict.
        """

        import json

        item = json.loads(raw)
        return item if isinstance(item, dict) else {"value": item}

    def _append_namespace(self, agent_id: str, namespace: str, payload: dict[str, Any]) -> str:
        """Append one namespace record.

        Input: agent id, namespace, and payload.
        Output: JSONL file path.
        """

        return self.service.put_namespace_item(self.service._namespace_tuple(agent_id, namespace), payload)

    def _agent_ids(self) -> list[str]:
        """List known agent namespaces.

        Input: none.
        Output: agent id list.
        """

        return self.service.list_agent_ids()

    @staticmethod
    def _parse_ts(value: Any) -> datetime | None:
        """Parse an ISO timestamp.

        Input: timestamp string.
        Output: timezone-aware datetime or None.
        """

        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(str(value))
            if parsed.tzinfo is None:
                return parsed.replace(tzinfo=timezone.utc)
            return parsed
        except Exception:
            return None

    def _build_thread_summary(self, items: list[dict[str, Any]]) -> str:
        """Build a compact thread summary.

        Input: episodic records.
        Output: summary text.
        """

        lines: list[str] = []
        for item in items[-6:]:
            text = str(item.get("text") or "").strip()
            if not text:
                continue
            lines.append(text[:240])
        if not lines:
            return "No episodic content available."
        return " | ".join(lines)

    @staticmethod
    def _normalize_text(text: str) -> str:
        """Normalize text for deduplication.

        Input: raw text.
        Output: normalized comparison string.
        """

        return " ".join((text or "").lower().split())

    def _dedupe_records(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Remove duplicate semantic candidates.

        Input: list of candidate records.
        Output: deduplicated list preserving order.
        """

        seen: set[str] = set()
        out: list[dict[str, Any]] = []
        for item in items:
            key = self._normalize_text(str(item.get("text") or ""))
            if not key or key in seen:
                continue
            seen.add(key)
            out.append(item)
        return out

    def _min_importance(self) -> float:
        """Return minimum promotion importance.

        Input: none.
        Output: float threshold.
        """

        return float(self.langmem_cfg.get("min_semantic_importance", 0.7))

    def _heuristic_importance(self, text: str, event: dict[str, Any]) -> float:
        """Estimate semantic importance from text cues.

        Input: text and event payload.
        Output: heuristic importance score.
        """

        lowered = text.lower()
        score = 0.0
        if any(token in lowered for token in ("prefer", "always", "never", "must", "remember", "should", "important", "avoid")):
            score += 0.5
        if any(token in lowered for token in ("like", "dislike", "need", "want", "rule", "preference")):
            score += 0.2
        if event.get("outcome") == "ok":
            score += 0.1
        if len(text) > 120:
            score += 0.1
        return min(1.0, score)
