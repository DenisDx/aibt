"""LLM factory: builds LangChain ChatOpenAI from config.json5 models section."""
from __future__ import annotations

from typing import Any

from langchain_openai import ChatOpenAI

from core.llm_wiretap import get_async_http_client
from core.llm_wiretap import get_sync_http_client


_UNSET = object()
_MODEL_REQUEST_PARAM_KEYS = (
    "temperature",
    "top_p",
    "repetition_penalty",
    "max_tokens",
    "seed",
    "presence_penalty",
    "frequency_penalty",
    "top_k",
    "min_p",
)
_DIRECT_REQUEST_PARAM_KEYS = (
    "temperature",
    "top_p",
    "max_tokens",
    "seed",
    "presence_penalty",
    "frequency_penalty",
)
_MODEL_KWARG_REQUEST_PARAM_KEYS = ("repetition_penalty", "top_k", "min_p")


def _first_model_id(provider_cfg: dict[str, Any]) -> str | None:
    models = provider_cfg.get("models", [])
    if isinstance(models, list) and models:
        first = models[0]
        if isinstance(first, dict):
            return str(first.get("id") or first.get("name") or "").strip() or None
    return None


def _model_entries(provider_cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """Return normalized provider model entry list."""

    models = provider_cfg.get("models", []) if isinstance(provider_cfg, dict) else []
    if not isinstance(models, list):
        return []
    return [item for item in models if isinstance(item, dict)]


def _find_model_entry(provider_cfg: dict[str, Any], model_name: str) -> dict[str, Any]:
    """Resolve one provider model entry by id or name."""

    clean_name = str(model_name or "").strip()
    if not clean_name:
        return {}
    for item in _model_entries(provider_cfg):
        item_id = str(item.get("id") or "").strip()
        item_name = str(item.get("name") or "").strip()
        if clean_name == item_id or clean_name == item_name:
            return item
    return {}


def _resolve_model_request_params(
    model_entry: dict[str, Any],
    *,
    temperature: Any = _UNSET,
    top_p: Any = _UNSET,
    repetition_penalty: Any = _UNSET,
    max_tokens: Any = _UNSET,
    seed: Any = _UNSET,
    presence_penalty: Any = _UNSET,
    frequency_penalty: Any = _UNSET,
    top_k: Any = _UNSET,
    min_p: Any = _UNSET,
) -> dict[str, Any]:
    """Merge model defaults with explicit overrides for request parameters."""

    overrides = {
        "temperature": temperature,
        "top_p": top_p,
        "repetition_penalty": repetition_penalty,
        "max_tokens": max_tokens,
        "seed": seed,
        "presence_penalty": presence_penalty,
        "frequency_penalty": frequency_penalty,
        "top_k": top_k,
        "min_p": min_p,
    }
    resolved: dict[str, Any] = {}
    for key in _MODEL_REQUEST_PARAM_KEYS:
        override_value = overrides[key]
        if override_value is not _UNSET:
            value = override_value
        else:
            value = model_entry.get(key) if isinstance(model_entry, dict) else None
        if value is None or value == "":
            continue
        resolved[key] = value
    return resolved


def build_llm(
    config: dict[str, Any],
    provider: str | None = None,
    model: str | None = None,
    tools: Any = _UNSET,
    temperature: Any = _UNSET,
    top_p: Any = _UNSET,
    repetition_penalty: Any = _UNSET,
    max_tokens: Any = _UNSET,
    seed: Any = _UNSET,
    presence_penalty: Any = _UNSET,
    frequency_penalty: Any = _UNSET,
    top_k: Any = _UNSET,
    min_p: Any = _UNSET,
) -> ChatOpenAI:
    """Build ChatOpenAI using active or explicitly overridden provider/model."""
    models_cfg = config.get("models", {}) if isinstance(config, dict) else {}
    providers = models_cfg.get("providers", {}) if isinstance(models_cfg, dict) else {}

    active_provider = str(provider or "").strip() or str(models_cfg.get("active_provider", "")).strip()
    if not active_provider and isinstance(providers, dict) and providers:
        active_provider = next(iter(providers.keys()))

    provider_cfg = providers.get(active_provider, {}) if active_provider else {}
    base_url = (
        str(provider_cfg.get("baseUrl", "")).strip()
        or None
    )
    api_key = (
        str(provider_cfg.get("apiKey", "")).strip()
    )
    model = (
        str(model or "").strip()
        or str(models_cfg.get("active_model", "")).strip()
        or _first_model_id(provider_cfg)
        or "gpt-4o-mini"
    )
    model_entry = _find_model_entry(provider_cfg, model)

    if not api_key:
        raise ValueError("LLM API key is missing. Set models.providers.*.apiKey in config.json5")

    request_params = _resolve_model_request_params(
        model_entry,
        temperature=temperature,
        top_p=top_p,
        repetition_penalty=repetition_penalty,
        max_tokens=max_tokens,
        seed=seed,
        presence_penalty=presence_penalty,
        frequency_penalty=frequency_penalty,
        top_k=top_k,
        min_p=min_p,
    )
    model_kwargs: dict[str, Any] = {}
    if tools is not _UNSET:
        model_kwargs["tools"] = tools
    for key in _MODEL_KWARG_REQUEST_PARAM_KEYS:
        if key in request_params:
            model_kwargs[key] = request_params[key]

    init_kwargs: dict[str, Any] = {
        "model": model,
        "api_key": api_key,
        "base_url": base_url,
        "http_client": get_sync_http_client(),
        "http_async_client": get_async_http_client(),
    }
    if model_kwargs:
        init_kwargs["model_kwargs"] = model_kwargs
    for key in _DIRECT_REQUEST_PARAM_KEYS:
        if key in request_params:
            init_kwargs[key] = request_params[key]

    return ChatOpenAI(**init_kwargs)
