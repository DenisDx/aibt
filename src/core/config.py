"""Config loading: reads config.json5, substitutes .env placeholders, returns dict."""
from __future__ import annotations
import os
import re
from typing import Any

try:
    import pyjson5 as _json5
except ImportError:
    raise ImportError("pyjson5 is required: pip install pyjson5")


def load_env_file(root_dir: str) -> dict[str, str]:
    """Load key=value pairs from .env. Returns empty dict if file is missing."""
    path = os.path.join(root_dir, ".env")
    env: dict[str, str] = {}
    if not os.path.exists(path):
        return env
    with open(path, encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            # Strip trailing inline comment (simple heuristic, not quote-aware)
            val = re.sub(r'\s+#[^"\']*$', '', val.strip())
            env[key.strip()] = val
    return env


def _substitute(text: str, env: dict[str, str]) -> str:
    """Replace ${VAR:-default} and ${VAR} using env dict then os.environ."""
    def _rep(m: re.Match) -> str:
        var, default = m.group(1), m.group(2)
        val = env.get(var) or os.environ.get(var)
        if val is None:
            return default if default is not None else ""
        return val
    return re.sub(r'\$\{([^}:]+)(?::-([^}]*))?\}', _rep, text)


def load_json5_text(text: str, env: dict[str, str] | None = None) -> dict[str, Any]:
    """Parse json5 text with ${VAR} substitution.

    Input: json5 text and optional env mapping.
    Output: parsed dict payload.
    """

    return _json5.loads(_substitute(text, env or {}))


def load_json5_file(path: str, env: dict[str, str] | None = None) -> dict[str, Any]:
    """Load one json5 file with variable substitution.

    Input: file path and optional env mapping.
    Output: parsed dict payload.
    """

    with open(path, encoding="utf-8") as f:
        return load_json5_text(f.read(), env=env)


def load_optional_json5_file(path: str, env: dict[str, str] | None = None) -> dict[str, Any]:
    """Load json5 file when present.

    Input: file path and optional env mapping.
    Output: parsed payload or empty dict if file is missing.
    """

    if not os.path.exists(path):
        return {}
    return load_json5_file(path, env=env)


def load_config(root_dir: str) -> dict:
    """Load and parse config.json5 with .env substitution. Returns config dict."""
    env = load_env_file(root_dir)
    config_path = os.path.join(root_dir, "config.json5")
    return load_json5_file(config_path, env=env)
