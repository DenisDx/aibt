#!/usr/bin/env python3
"""Memoryd smoke test.

Runs one read pass and one LLM-driven write pass against memoryd using a chosen model.
Execute with: ./venv/bin/python scripts/memoryd_smoke.py ...
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any


ROOT_DIR = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from core.config import load_config
from core.envid_runtime import build_effective_config
from core.logging_utils import init_logging
from memoryd import get_memoryd_service


def _parse_types(raw: str) -> list[str]:
    items = [item.strip() for item in raw.split(",")]
    return [item for item in items if item]


def _build_smoke_request_text(types: list[str], title: str) -> str:
    type_name = types[0] if types else "semantic"
    return (
        "You are a memoryd smoke-test worker. Return JSON only.\n"
        "Emit exactly one mutation in a JSON array.\n"
        "The mutation must be an INSERT using the following rules:\n"
        f"- type: {type_name}\n"
        f"- title: {title}\n"
        "- text: use FINAL_RESPONSE as the body\n"
        "- importance: 5\n"
        "Do not add commentary, markdown fences, or extra keys.\n"
    )


def _normalize_types_list(raw: list[str] | None) -> list[str]:
    return [item.strip().lower() for item in (raw or []) if item and item.strip()]


def _enabled_memoryd_types(memoryd_cfg: dict[str, Any]) -> list[str]:
    items = memoryd_cfg.get("items", {}) if isinstance(memoryd_cfg, dict) else {}
    if not isinstance(items, dict):
        return []
    out: list[str] = []
    for type_name, cfg in items.items():
        if isinstance(cfg, dict) and cfg.get("enabled", False):
            name = str(type_name).strip().lower()
            if name:
                out.append(name)
    return sorted(set(out))


def _find_request_group(memoryd_cfg: dict[str, Any], types: list[str]) -> dict[str, Any] | None:
    groups = memoryd_cfg.get("requests", []) if isinstance(memoryd_cfg, dict) else []
    if not isinstance(groups, list):
        return None
    normalized = sorted(set(_normalize_types_list(types)))
    for group in groups:
        if not isinstance(group, dict):
            continue
        group_types = sorted(set(_normalize_types_list(group.get("types"))))
        if group_types == normalized:
            return group
    if len(groups) == 1 and isinstance(groups[0], dict):
        return groups[0]
    return None


def _resolve_request_file(config: dict[str, Any], types: list[str], explicit_request_file: str | None) -> tuple[str, bool]:
    if explicit_request_file:
        return explicit_request_file, True
    memoryd_cfg = config.get("memoryd", {}) if isinstance(config, dict) else {}
    group = _find_request_group(memoryd_cfg, types)
    if group:
        request_file = str(group.get("request_file") or "").strip()
        if request_file:
            return request_file, True
    return "", False


def _find_record(service, muid: str, title: str, types: list[str]) -> dict[str, Any] | None:
    page = service.list_records(muid=muid, types=types, offset=0, limit=100)
    for item in page.get("items", []):
        if str(item.get("title") or "").strip() == title:
            return item
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Memoryd read/write smoke test")
    parser.add_argument("--muid", default="smoke", help="Memory namespace to test")
    parser.add_argument("--types", default="", help="Comma-separated memoryd types")
    parser.add_argument("--provider", default="", help="LLM provider override")
    parser.add_argument("--model", default="", help="LLM model override")
    parser.add_argument("--envid", default="", help="Optional envid overlay to apply before the test")
    parser.add_argument("--request-file", default="", help="Optional explicit memoryd request file")
    parser.add_argument("--title", default="memoryd smoke record", help="Record title used by the smoke write")
    parser.add_argument("--body", default="memoryd smoke body", help="Record text used by the smoke write")
    parser.add_argument("--caller-tag", default="memoryd-smoke", help="Caller tag for the queued task")
    parser.add_argument("--priority", type=int, default=8, help="Task priority used for the smoke run")
    parser.add_argument("--max-record-count", type=int, default=5, help="Retention limit per type")
    parser.add_argument("--max-content-length", type=int, default=8196, help="Retention content limit per type")
    parser.add_argument("--render", choices=("markdown", "text"), default="markdown", help="Render mode for context output")
    parser.add_argument("--root", default=str(ROOT_DIR), help="Project root directory")
    args = parser.parse_args()

    root_dir = Path(args.root).resolve()
    config = load_config(str(root_dir))
    if args.envid:
        config = build_effective_config(config, args.envid)
    init_logging(config, str(root_dir))

    memoryd_cfg = config.get("memoryd", {}) if isinstance(config, dict) else {}
    types = _parse_types(args.types)
    if not types:
        group = _find_request_group(memoryd_cfg, [])
        if group:
            types = _normalize_types_list(group.get("types"))
    if not types:
        types = _enabled_memoryd_types(memoryd_cfg)
    if not types:
        raise SystemExit("At least one memoryd type must be provided or enabled in config")

    request_file, from_config = _resolve_request_file(config, types, args.request_file)
    if not request_file:
        request_text = _build_smoke_request_text(types, args.title)
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False, suffix=".md") as handle:
            handle.write(request_text)
            request_file = handle.name
        from_config = False

    try:
        memoryd_cfg = config.setdefault("memoryd", {})
        memoryd_cfg["enabled"] = True
        if args.provider:
            memoryd_cfg["provider"] = args.provider
        if args.model:
            memoryd_cfg["model"] = args.model
        memoryd_cfg["muid"] = args.muid
        memoryd_cfg["max_sim_task"] = 1
        memoryd_cfg["memory_task_prio"] = args.priority
        if not from_config:
            memoryd_cfg["requests"] = [
                {
                    "types": types,
                    "request_file": request_file,
                }
            ]
            memoryd_cfg["items"] = {
                type_name: {
                    "enabled": True,
                    "max_record_count": args.max_record_count,
                    "max_content_length": args.max_content_length,
                }
                for type_name in types
            }

        service = get_memoryd_service(str(root_dir), config)
        service.initialize()

        before_context = service.get_context(args.muid, types=types, render=args.render)
        print("=== BEFORE ===")
        print(before_context["text"] or "<empty>")

        enqueue_result = service.enqueue_update(
            source_context={
                "smoke_mode": True,
                "title": args.title,
                "body": args.body,
                "types": types,
            },
            final_response=args.body,
            muid=args.muid,
            caller_tag=args.caller_tag,
            types=types,
        )
        print("=== ENQUEUE ===")
        print(json.dumps(enqueue_result, ensure_ascii=False, indent=2))

        tick_result = service.run_tick(limit=1)
        print("=== TICK ===")
        print(json.dumps(tick_result, ensure_ascii=False, indent=2))

        record = _find_record(service, args.muid, args.title, types)
        after_context = service.get_context(args.muid, types=types, render=args.render)
        print("=== AFTER ===")
        print(after_context["text"] or "<empty>")

        if record is None:
            print("Smoke test did not find the expected record after the tick.", file=sys.stderr)
            return 2

        print("=== MATCH ===")
        print(json.dumps(record, ensure_ascii=False, indent=2, default=str))
        return 0
    finally:
        try:
            os.unlink(request_file)
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())