from __future__ import annotations

from typing import Protocol

from app.rag.bm25 import tokenize


class RerankerAdapter(Protocol):
    async def score(self, query: str, document: str) -> float: ...


class LexicalReranker:
    """Deterministic overlap reranker used until BGE reranker is enabled."""

    async def score(self, query: str, document: str) -> float:
        query_tokens = set(tokenize(query))
        if not query_tokens:
            return 0.0
        document_tokens = set(tokenize(document))
        return len(query_tokens & document_tokens) / len(query_tokens)