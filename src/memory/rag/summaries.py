"""Summary helpers for indexed documents."""

from __future__ import annotations


def make_summary(text: str, max_chars: int = 400) -> str:
    """Build compact summary from text.

    Input: full text and max characters.
    Output: short summary string.
    """

    clean = " ".join((text or "").split())
    if len(clean) <= max_chars:
        return clean
    return clean[: max_chars - 3].rstrip() + "..."
