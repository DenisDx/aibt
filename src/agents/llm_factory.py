"""LLM factory: builds LangChain ChatOpenAI from config.json5 models section."""
from __future__ import annotations

from typing import Any

from langchain_openai import ChatOpenAI

from core.llm_wiretap import get_async_http_client


_UNSET = object()


def _first_model_id(provider_cfg: dict[str, Any]) -> str | None:
    models = provider_cfg.get("models", [])
    if isinstance(models, list) and models:
        first = models[0]
        if isinstance(first, dict):
            return str(first.get("id") or first.get("name") or "").strip() or None
    return None


def build_llm(
    config: dict[str, Any],
    provider: str | None = None,
    model: str | None = None,
    tools: Any = _UNSET,
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

    if not api_key:
        raise ValueError("LLM API key is missing. Set models.providers.*.apiKey in config.json5")

    model_kwargs: dict[str, Any] = {}
    if tools is not _UNSET:
        model_kwargs["tools"] = tools

    return ChatOpenAI(
        model=model,
        api_key=api_key,
        base_url=base_url,
        temperature=0,
        model_kwargs=model_kwargs,
        http_async_client=get_async_http_client(),
    )
