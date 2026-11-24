from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.embed import EmbeddingAdapter, cosine
from app.adapters.rerank import RerankerAdapter
from app.rag.bm25 import BM25
from app.rag.filters import RetrievalScope, load_scoped_chunks
from app.rag.rrf import reciprocal_rank_fusion


@dataclass
class Evidence:
    chunk_id: str
    doc_id: str
    doc_title: str
    version: int
    text: str
    bm25_score: float
    vector_score: float
    rrf_score: float
    rerank_score: float

    def to_dict(self) -> dict:
        return asdict(self)


async def retrieve_evidence(
    session: AsyncSession,
    scope: RetrievalScope,
    query: str,
    embedder: EmbeddingAdapter,
    reranker: RerankerAdapter,
    top_k: int = 5,
) -> list[Evidence]:
    chunks = await load_scoped_chunks(session, scope)
    if not chunks:
        return []

    texts = [chunk["text"] for chunk in chunks]
    bm25_scores = BM25(texts).scores(query)
    bm25_ranked = [
        chunk["chunk_id"]
        for chunk, _score in sorted(
            zip(chunks, bm25_scores), key=lambda pair: pair[1], reverse=True
        )
        if _score > 0
    ]

    query_embedding = await embedder.embed(query)
    chunk_embeddings = await asyncio.gather(
        *[embedder.embed(text) for text in texts]
    )
    vector_scores = [
        cosine(query_embedding, embedding) for embedding in chunk_embeddings
    ]
    vector_ranked = [
        chunk["chunk_id"]
        for chunk, _score in sorted(
            zip(chunks, vector_scores), key=lambda pair: pair[1], reverse=True
        )
    ]

    fused = reciprocal_rank_fusion([bm25_ranked, vector_ranked])
    by_id = {chunk["chunk_id"]: chunk for chunk in chunks}
    bm25_by_id = {
        chunk["chunk_id"]: score for chunk, score in zip(chunks, bm25_scores)
    }
    vector_by_id = {
        chunk["chunk_id"]: score for chunk, score in zip(chunks, vector_scores)
    }
    rrf_by_id = dict(fused)

    rerank_scores = await asyncio.gather(
        *[reranker.score(query, chunk["text"]) for chunk in chunks]
    )
    rerank_by_id = {
        chunk["chunk_id"]: score for chunk, score in zip(chunks, rerank_scores)
    }

    ranked = sorted(
        fused,
        key=lambda pair: (rerank_by_id.get(pair[0], 0.0), pair[1]),
        reverse=True,
    )

    evidence: list[Evidence] = []
    for chunk_id, rrf_score in ranked[:top_k]:
        chunk = by_id[chunk_id]
        evidence.append(
            Evidence(
                chunk_id=chunk_id,
                doc_id=chunk["doc_id"],
                doc_title=chunk["doc_title"],
                version=chunk["version"],
                text=chunk["text"],
                bm25_score=round(bm25_by_id.get(chunk_id, 0.0), 4),
                vector_score=round(vector_by_id.get(chunk_id, 0.0), 4),
                rrf_score=round(rrf_by_id.get(chunk_id, 0.0), 6),
                rerank_score=round(rerank_by_id.get(chunk_id, 0.0), 4),
            )
        )
    return evidence