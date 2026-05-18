"""Config loading: reads config.json5, substitutes .env placeholders, returns dict."""
from __future__ import annotations
import os
import re

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


def load_config(root_dir: str) -> dict:
    """Load and parse config.json5 with .env substitution. Returns config dict."""
    env = load_env_file(root_dir)
    config_path = os.path.join(root_dir, "config.json5")
    with open(config_path, encoding="utf-8") as f:
        text = f.read()
    return _json5.loads(_substitute(text, env))
