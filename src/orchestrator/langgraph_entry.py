"""LangGraph CLI entrypoint for the aibt orchestrator graph."""
from __future__ import annotations

import os
import sys

# Resolve project root and import src modules.
_ROOT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_SRC_DIR = os.path.join(_ROOT_DIR, "src")
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from core.config import load_config
from orchestrator.orchestrator import AgentOrchestrator


# Compiled LangGraph used by `langgraph dev`.
graph = AgentOrchestrator(load_config(_ROOT_DIR)).graph
