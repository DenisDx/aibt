"""LLM factory: builds LangChain ChatOpenAI from config.json5 models section."""
from __future__ import annotations

import os
from typing import Any

from langchain_openai import ChatOpenAI
from core.config import load_env_file


def _first_model_id(provider_cfg: dict[str, Any]) -> str | None:
    models = provider_cfg.get("models", [])
    if isinstance(models, list) and models:
        first = models[0]
        if isinstance(first, dict):
            return str(first.get("id") or first.get("name") or "").strip() or None
    return None


def build_llm(config: dict[str, Any]) -> ChatOpenAI:
    """Build ChatOpenAI using active provider/model from config."""
    models_cfg = config.get("models", {}) if isinstance(config, dict) else {}
    providers = models_cfg.get("providers", {}) if isinstance(models_cfg, dict) else {}
    root_dir = str(config.get("root", "")) if isinstance(config, dict) else ""
    file_env = load_env_file(root_dir) if root_dir else {}

    active_provider = str(models_cfg.get("active_provider", "")).strip()
    if not active_provider and isinstance(providers, dict) and providers:
        active_provider = next(iter(providers.keys()))

    provider_cfg = providers.get(active_provider, {}) if active_provider else {}
    base_url = (
        str(provider_cfg.get("baseUrl", "")).strip()
        or file_env.get("LLM_BASE_URL", "")
        or os.getenv("LLM_BASE_URL")
        or None
    )
    api_key = (
        str(provider_cfg.get("apiKey", "")).strip()
        or file_env.get("LLM_API_KEY", "")
        or os.getenv("LLM_API_KEY")
        or os.getenv("OPENAI_API_KEY", "")
    )
    model = (
        str(models_cfg.get("active_model", "")).strip()
        or _first_model_id(provider_cfg)
        or file_env.get("LLM_MODEL", "")
        or os.getenv("LLM_MODEL")
        or "gpt-4o-mini"
    )

    if not api_key:
        raise ValueError("LLM API key is missing. Set models.providers.*.apiKey or LLM_API_KEY/OPENAI_API_KEY in .env")

    return ChatOpenAI(
        model=model,
        api_key=api_key,
        base_url=base_url,
        temperature=0,
    )
