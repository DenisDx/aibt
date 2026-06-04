"""Memoryd schemas and normalization helpers.

Provides: request/record normalization utilities for memoryd storage and queueing.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any

from core.logging_utils import log


DEFAULT_MEMORYD_LIMIT_PER_TYPE = 20
DEFAULT_MEMORYD_TASK_LIMIT = 1
MAX_MEMORYD_MUID_LENGTH = 128


def _preview_data_block(value: Any, head: int = 100, tail: int = 100) -> str:
    """Build bounded preview string: up to 100 chars from start + 100 from end.

    Input: any payload value.
    Output: preview string with max length 200.
    """

    text = str(value or "")
    cap = max(0, int(head)) + max(0, int(tail))
    if len(text) <= cap:
        return text
    return text[: max(0, int(head))] + text[-max(0, int(tail)) :]


def clean_text(value: Any) -> str:
    """Normalize any value into a trimmed lower-case string.

    Input: raw value.
    Output: normalized string.
    """

    return str(value or "").strip().lower()


def normalize_types(types: list[Any] | None, allowed_types: list[str] | None = None) -> list[str]:
    """Normalize memory types for exact-set matching.

    Input: requested types and optional allowlist.
    Output: sorted unique normalized types.
    """

    log("memoryd", "debug", "call memoryd.schemas.normalize_types")
    if types is None:
        return sorted({clean_text(t) for t in (allowed_types or []) if clean_text(t)})

    allowed = {clean_text(t) for t in (allowed_types or []) if clean_text(t)} if allowed_types else None
    out: set[str] = set()
    for raw in types:
        item = clean_text(raw)
        if not item:
            continue
        if allowed is not None and item not in allowed:
            continue
        out.add(item)
    return sorted(out)


def split_memoryd_type_spec(value: Any) -> tuple[str | None, str]:
    """Split optional `muid:type` spec into normalized parts.

    Input: raw type spec.
    Output: (muid or None, type name).
    """

    text = clean_text(value)
    if not text:
        return (None, "")
    if ":" not in text:
        return (None, text)
    muid_part, type_part = text.split(":", 1)
    muid = clean_text(muid_part) or None
    type_name = clean_text(type_part)
    return (muid, type_name)


def normalize_memoryd_type_spec(value: Any, allowed_types: list[str] | None = None) -> str | None:
    """Normalize one memoryd type spec with optional base-type filtering.

    Input: raw `type` or `muid:type` spec and optional allowed base types.
    Output: normalized spec or None when invalid/not allowed.
    """

    log("memoryd", "debug", "call memoryd.schemas.normalize_memoryd_type_spec")
    muid, type_name = split_memoryd_type_spec(value)
    if not type_name:
        return None
    allowed = {clean_text(t) for t in (allowed_types or []) if clean_text(t)} if allowed_types else None
    if allowed is not None and type_name not in allowed:
        return None
    if muid:
        return f"{muid}:{type_name}"
    return type_name


def normalize_memoryd_type_specs(values: list[Any] | None, allowed_types: list[str] | None = None) -> list[str]:
    """Normalize ordered unique memoryd type specs.

    Input: list of `type` or `muid:type` entries and optional allowed base types.
    Output: normalized unique specs in input order.
    """

    log("memoryd", "debug", "call memoryd.schemas.normalize_memoryd_type_specs")
    if not isinstance(values, list):
        return []
    out: list[str] = []
    for raw in values:
        spec = normalize_memoryd_type_spec(raw, allowed_types=allowed_types)
        if spec and spec not in out:
            out.append(spec)
    return out


def memoryd_type_names(values: list[Any] | None, allowed_types: list[str] | None = None) -> list[str]:
    """Extract unique normalized base type names from specs.

    Input: list of `type` or `muid:type` entries.
    Output: normalized base type names in stable order.
    """

    log("memoryd", "debug", "call memoryd.schemas.memoryd_type_names")
    out: list[str] = []
    for spec in normalize_memoryd_type_specs(values, allowed_types=allowed_types):
        _, type_name = split_memoryd_type_spec(spec)
        if type_name and type_name not in out:
            out.append(type_name)
    return out


def resolve_memoryd_type_specs(
    values: list[Any] | None,
    *,
    default_muid: Any,
    allowed_types: list[str] | None = None,
) -> list[dict[str, str]]:
    """Resolve type specs into concrete `(muid, type)` targets.

    Input: spec list, default muid, and optional allowed base types.
    Output: ordered resolved targets with `spec`, `muid`, and `type` keys.
    """

    log("memoryd", "debug", "call memoryd.schemas.resolve_memoryd_type_specs")
    fallback_muid = normalize_muid(default_muid)
    out: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for spec in normalize_memoryd_type_specs(values, allowed_types=allowed_types):
        spec_muid, type_name = split_memoryd_type_spec(spec)
        resolved_muid = normalize_muid(spec_muid or fallback_muid)
        if not resolved_muid or not type_name:
            continue
        key = (resolved_muid, type_name)
        if key in seen:
            continue
        seen.add(key)
        out.append({"spec": spec, "muid": resolved_muid, "type": type_name})
    return out


def normalize_muid(muid: Any) -> str:
    """Normalize MUID to canonical lowercase text.

    Input: raw MUID.
    Output: canonical MUID string.
    """

    log("memoryd", "debug", "call memoryd.schemas.normalize_muid")
    return clean_text(muid)


def normalize_json_value(value: Any) -> Any:
    """Make value JSON-safe for storage.

    Input: arbitrary value.
    Output: JSON-serializable payload.
    """

    if value is None or isinstance(value, (str, int, float, bool)):
        return value

    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            out[str(key)] = normalize_json_value(item)
        return out

    if isinstance(value, (list, tuple, set)):
        return [normalize_json_value(item) for item in value]

    # Common model objects (pydantic/langchain/etc.) often expose model_dump().
    if hasattr(value, "model_dump"):
        try:
            return normalize_json_value(value.model_dump())
        except Exception:
            pass

    # Some objects expose dict() but are not plain mappings.
    if hasattr(value, "dict") and callable(getattr(value, "dict", None)):
        try:
            return normalize_json_value(value.dict())
        except Exception:
            pass

    return str(value)


def serialize_json(value: Any) -> str:
    """Serialize value using standard JSON.

    Input: JSON-compatible payload.
    Output: JSON text.
    """

    log("memoryd", "debug", "call memoryd.schemas.serialize_json")
    return json.dumps(normalize_json_value(value), ensure_ascii=False, separators=(",", ":"), default=str)


def deserialize_json(value: Any, default: Any = None) -> Any:
    """Deserialize JSON text or pass-through Python value.

    Input: value and default fallback.
    Output: Python value.
    """

    log("memoryd", "debug", "call memoryd.schemas.deserialize_json")
    if value is None:
        return default
    if isinstance(value, (dict, list, int, float, bool)):
        return value
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8", errors="replace")
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception as e:
            preview = _preview_data_block(value)
            log(
                "memoryd",
                "warning",
                f"invalid JSON value format in deserialize_json: {e}; preview_len={len(preview)} raw_len={len(value)} preview={preview}",
            )
            return default if default is not None else value
    return default if default is not None else value


@dataclass(slots=True)
class MemorydTaskMutation:
    """Normalized mutation instruction for worker application.

    Input: parsed mutation dict.
    Output: normalized mutation fields.
    """

    operation: str
    type_name: str | None = None
    record_id: str | None = None
    title: str | None = None
    text: str | None = None
    importance: int = 5
