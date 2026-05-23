"""Memory package entrypoint.

Provides: factory helpers for the memory facade.
"""

from memory.api import MemoryService, get_memory_service

__all__ = ["MemoryService", "get_memory_service"]
