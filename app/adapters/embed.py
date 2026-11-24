from __future__ import annotations

import hashlib
import math
from typing import Protocol


class EmbeddingAdapter(Protocol):
    async def embed(self, text: str) -> list[float]: ...


class HashEmbeddingAdapter:
    """Deterministic feature-hashing embedding; no model download required."""

    def __init__(self, dims: int = 64):
        self.dims = dims

    async def embed(self, text: str) -> list[float]:
        vector = [0.0] * self.dims
        for token in _tokens(text):
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            index = int.from_bytes(digest[:4], "big") % self.dims
            sign = 1.0 if digest[4] % 2 == 0 else -1.0
            vector[index] += sign
        norm = math.sqrt(sum(value * value for value in vector)) or 1.0
        return [value / norm for value in vector]


def _tokens(text: str) -> list[str]:
    text = text.lower()
    tokens = [character for character in text if character.strip()]
    tokens.extend(
        text[index : index + 2] for index in range(max(0, len(text) - 1))
    )
    return tokens


def cosine(left: list[float], right: list[float]) -> float:
    if not left or not right:
        return 0.0
    return sum(a * b for a, b in zip(left, right))