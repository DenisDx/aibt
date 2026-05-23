"""Core runtime helpers for envid resolution and config overlay assembly."""

from __future__ import annotations

from collections import OrderedDict
from copy import deepcopy
import os
from typing import Any

from core.config import load_env_file, load_optional_json5_file


def deep_merge_config(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Deep merge two config dicts.

    Input: base and overlay dicts.
    Output: merged dict where overlay wins.
    """

    result = deepcopy(base)
    for key, value in (overlay or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge_config(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result


def _envid_items(config: dict[str, Any]) -> dict[str, Any]:
    """Read envid items map from root config.

    Input: root config.
    Output: envid id -> config dict.
    """

    envids_cfg = config.get("envids", {}) if isinstance(config, dict) else {}
    items = envids_cfg.get("items", {}) if isinstance(envids_cfg, dict) else {}
    return items if isinstance(items, dict) else {}


def load_environment_registry(config: dict[str, Any], adapter_name: str | None = None) -> "OrderedDict[str, dict[str, Any]]":
    """Build deterministic envid registry from config.

    Input: root config and optional adapter name.
    Output: ordered mapping envid -> envid entry.
    """

    envids_cfg = config.get("envids", {}) if isinstance(config, dict) else {}
    enabled = bool(envids_cfg.get("enabled", False))
    items = _envid_items(config)
    registry: "OrderedDict[str, dict[str, Any]]" = OrderedDict()
    if not enabled or not items:
        return registry

    explicit_order: list[str] = []
    adapter_orders = envids_cfg.get("adapter_startup_order", {}) if isinstance(envids_cfg, dict) else {}
    if adapter_name and isinstance(adapter_orders, dict):
        raw = adapter_orders.get(adapter_name)
        if isinstance(raw, list):
            explicit_order = [str(x).strip() for x in raw if str(x).strip()]

    if not explicit_order:
        raw = envids_cfg.get("startup_order") if isinstance(envids_cfg, dict) else None
        if isinstance(raw, list):
            explicit_order = [str(x).strip() for x in raw if str(x).strip()]

    seen: set[str] = set()
    for envid in explicit_order:
        if envid in items and envid not in seen:
            registry[envid] = items[envid] if isinstance(items[envid], dict) else {}
            seen.add(envid)

    for envid, value in items.items():
        key = str(envid).strip()
        if not key or key in seen:
            continue
        registry[key] = value if isinstance(value, dict) else {}
        seen.add(key)

    return registry


def _as_set(values: Any) -> set[str]:
    """Normalize scalar/list values to a lowercase string set."""

    if values is None:
        return set()
    if isinstance(values, (list, tuple, set)):
        return {str(v).strip().lower() for v in values if str(v).strip()}
    text = str(values).strip()
    return {text.lower()} if text else set()


def _match_adapter_rules(adapter_name: str, rules: dict[str, Any], event_context: dict[str, Any]) -> bool:
    """Check adapter matching rules against one event context."""

    if not isinstance(rules, dict):
        return False
    adapters = rules.get("adapters", {}) if isinstance(rules, dict) else {}
    adapter_rules = adapters.get(adapter_name, {}) if isinstance(adapters, dict) else {}
    if not isinstance(adapter_rules, dict) or not adapter_rules:
        return False

    if adapter_name == "telegram":
        chat_id = str(event_context.get("chat_id", "")).strip()
        username = str(event_context.get("chat_username", "")).strip().lstrip("@").lower()
        chat_type = str(event_context.get("chat_type", "")).strip().lower()

        raw_chat_ids = adapter_rules.get("chat_ids")
        raw_usernames = adapter_rules.get("chat_usernames")
        raw_types = adapter_rules.get("chat_types")

        if isinstance(raw_chat_ids, list) and raw_chat_ids:
            allow_ids = {str(v).strip() for v in raw_chat_ids if str(v).strip()}
            if chat_id not in allow_ids:
                return False

        if isinstance(raw_usernames, list) and raw_usernames:
            allow_names = {str(v).strip().lstrip("@").lower() for v in raw_usernames if str(v).strip()}
            if username not in allow_names:
                return False

        if isinstance(raw_types, list) and raw_types:
            allow_types = {str(v).strip().lower() for v in raw_types if str(v).strip()}
            if chat_type not in allow_types:
                return False

        return True

    adapter_id = str(event_context.get("adapter_id", "")).strip()
    if adapter_id:
        allowed_ids = _as_set(adapter_rules.get("adapter_ids"))
        if allowed_ids and adapter_id.lower() not in allowed_ids:
            return False

    return True


def resolve_envid(
    config: dict[str, Any],
    adapter_name: str,
    event_context: dict[str, Any] | None = None,
    explicit_envid: str | None = None,
) -> str | None:
    """Resolve envid for one runtime event.

    Input: root config, adapter name, event context, optional explicit envid.
    Output: resolved envid or None if fallback to base config is allowed.
    """

    ctx = event_context or {}
    registry = load_environment_registry(config, adapter_name=adapter_name)
    envids_cfg = config.get("envids", {}) if isinstance(config, dict) else {}
    strict = bool(envids_cfg.get("strict_matching", False))

    candidate = str(explicit_envid or ctx.get("envid") or "").strip()
    if candidate:
        if candidate in registry:
            return candidate
        raise ValueError(f"envid '{candidate}' is not declared in envids.items")

    for envid, entry in registry.items():
        runtime_cfg = entry.get("runtime", {}) if isinstance(entry, dict) else {}
        if isinstance(runtime_cfg, dict) and runtime_cfg.get("enabled") is False:
            continue
        matching = entry.get("matching", {}) if isinstance(entry, dict) else {}
        if _match_adapter_rules(adapter_name, matching, ctx):
            return envid

    if strict:
        raise ValueError(f"No matching envid found for adapter '{adapter_name}'")
    return None


def build_effective_config(root_config: dict[str, Any], envid: str | None) -> dict[str, Any]:
    """Build effective config by applying selected envid overlay to base config.

    Input: root config and optional resolved envid.
    Output: effective root config.
    """

    base = deepcopy(root_config or {})
    if not envid:
        return base

    items = _envid_items(base)
    env_entry = items.get(envid, {}) if isinstance(items, dict) else {}
    overlay = env_entry.get("config", {}) if isinstance(env_entry, dict) else {}
    if not isinstance(overlay, dict):
        return base

    merged = deep_merge_config(base, overlay)
    if isinstance(merged.get("envids"), dict):
        merged["envids"] = deepcopy(base.get("envids", {}))
    return merged


def _component_paths(component_type: str, component_id: str, root_dir: str) -> tuple[str, tuple[str, ...]]:
    """Resolve local config file path and config section path for one component."""

    if component_type == "agent":
        cfg_path = os.path.join(root_dir, "src", "agents", component_id, "config.json5")
        section = ("agents", "items", component_id)
        return cfg_path, section
    if component_type == "adapter":
        cfg_path = os.path.join(root_dir, "src", "adapters", component_id, "config.json5")
        section = ("adapters", "items", component_id)
        return cfg_path, section
    raise ValueError(f"Unsupported component_type '{component_type}'")


def _mount_section(section_path: tuple[str, ...], payload: dict[str, Any]) -> dict[str, Any]:
    """Mount payload under nested section path."""

    if not isinstance(payload, dict) or not section_path:
        return {}
    out: dict[str, Any] = {}
    current = out
    for part in section_path[:-1]:
        current[part] = {}
        current = current[part]
    current[section_path[-1]] = deepcopy(payload)
    return out


def assemble_component_config(
    component_type: str,
    component_id: str,
    envid: str | None,
    root_config: dict[str, Any],
    local_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble final component section config via shared overlay rules.

    Input: component type/id, resolved envid, root config, optional local config payload.
    Output: final component config section.
    """

    clean_id = str(component_id or "").strip()
    if not clean_id:
        return {}

    root = root_config or {}
    root_dir = str(root.get("root") or "").strip()
    _, section_path = _component_paths(component_type, clean_id, root_dir)

    payload = local_config
    if payload is None and root_dir:
        env = load_env_file(root_dir)
        local_path, _ = _component_paths(component_type, clean_id, root_dir)
        payload = load_optional_json5_file(local_path, env=env)
    if not isinstance(payload, dict):
        payload = {}

    local_layer = _mount_section(section_path, payload)
    merged = deep_merge_config(local_layer, root)

    if envid:
        items = _envid_items(root)
        env_entry = items.get(envid, {}) if isinstance(items, dict) else {}
        overlay = env_entry.get("config", {}) if isinstance(env_entry, dict) else {}
        if isinstance(overlay, dict):
            merged = deep_merge_config(merged, overlay)

    section: Any = merged
    for part in section_path:
        section = section.get(part, {}) if isinstance(section, dict) else {}
    return section if isinstance(section, dict) else {}
