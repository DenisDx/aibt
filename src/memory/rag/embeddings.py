"""Deterministic local embeddings for document chunks and queries."""

from __future__ import annotations

import hashlib
import math
import re


DEFAULT_EMBEDDING_DIM = 256


def tokenize(text: str) -> list[str]:
    """Tokenize input text into lower-cased word pieces.

    Input: arbitrary text.
    Output: token list.
    """

    return re.findall(r"[a-z0-9_]+", (text or "").lower())


def text_to_embedding(text: str, dim: int = DEFAULT_EMBEDDING_DIM) -> list[float]:
    """Convert text into a normalized dense vector.

    Input: text and vector dimension.
    Output: normalized float vector.
    """

    safe_dim = max(32, int(dim))
    vec = [0.0] * safe_dim
    tokens = tokenize(text)
    if not tokens:
        return vec

    for pos, token in enumerate(tokens):
        digest = hashlib.sha256(f"{pos}:{token}".encode("utf-8")).digest()
        for offset in range(0, 32, 4):
            idx = int.from_bytes(digest[offset : offset + 4], "big") % safe_dim
            sign = 1.0 if digest[offset] % 2 == 0 else -1.0
            vec[idx] += sign

    norm = math.sqrt(sum(v * v for v in vec))
    if norm <= 0:
        return vec
    return [v / norm for v in vec]


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Compute cosine similarity between dense vectors.

    Input: two vectors of equal length.
    Output: cosine similarity score.
    """

    if not a or not b or len(a) != len(b):
        return 0.0
    return float(sum(x * y for x, y in zip(a, b)))


def embedding_to_vector_literal(vec: list[float]) -> str:
    """Format vector literal for pgvector SQL casts.

    Input: dense vector.
    Output: pgvector-compatible text literal.
    """

    return "[" + ",".join(f"{float(v):.8f}" for v in vec) + "]"
