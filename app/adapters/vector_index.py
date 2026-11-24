from __future__ import annotations

from typing import Protocol

from app.adapters.integrations import NotConfigured
from app.config import Settings


class VectorIndex(Protocol):
    async def build(self, collection: str, documents: list[dict]) -> dict: ...


class FakeVectorIndex:
    """In-process index so publishing and rollback work without Milvus."""

    def __init__(self, *, fail_on_build: bool = False):
        self.fail_on_build = fail_on_build
        self.collections: dict[str, list[dict]] = {}
        self.build_calls: list[dict] = []

    async def build(self, collection: str, documents: list[dict]) -> dict:
        self.build_calls.append({"collection": collection, "count": len(documents)})
        if self.fail_on_build:
            raise RuntimeError("fake vector index build failure")
        self.collections[collection] = [dict(document) for document in documents]
        return {"collection": collection, "count": len(documents)}


class MilvusVectorIndex:
    """Milvus contract adapter; raises NotConfigured until pymilvus is provisioned."""

    def __init__(self, settings: Settings):
        self.settings = settings

    async def build(self, collection: str, documents: list[dict]) -> dict:
        if not self.settings.milvus_enabled:
            raise NotConfigured("Milvus integration is disabled")
        if not self.settings.milvus_uri:
            raise NotConfigured("MILVUS_URI is not configured")
        try:
            import pymilvus  # type: ignore # noqa: F401
        except ImportError as exc:
            raise NotConfigured(
                "pymilvus is not installed; Milvus publishing is unavailable"
            ) from exc
        raise NotConfigured(
            "Milvus live publishing is not enabled in this local delivery"
        )


def build_vector_index(settings: Settings) -> VectorIndex:
    if settings.milvus_enabled and settings.milvus_uri:
        return MilvusVectorIndex(settings)
    return FakeVectorIndex()