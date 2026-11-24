from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.adapters.integrations import NotConfigured
from app.adapters.vector_index import (
    FakeVectorIndex,
    MilvusVectorIndex,
    build_vector_index,
)
from app.config import Settings
from app.models import (
    IndexSnapshot,
    KnowledgeChunk,
    KnowledgeDoc,
    KnowledgeStatus,
    KnowledgeVersion,
    Tenant,
)
from app.security import content_hash
from app.services.index_publish import (
    IndexPublishError,
    active_snapshot,
    publish_index,
    rollback_index,
)
from app.trace import utcnow


async def seed_versions(sessionmaker):
    async with sessionmaker() as session:
        tenant = Tenant(code=f"IDX-{uuid.uuid4().hex[:8]}", name="Index Tenant")
        session.add(tenant)
        await session.flush()
        doc = KnowledgeDoc(
            tenant_id=tenant.id,
            store_id=None,
            marketplace="JP",
            language="zh",
            title="索引发布测试文档",
            status=KnowledgeStatus.published,
        )
        session.add(doc)
        await session.flush()

        versions = []
        for number, status in (
            (1, KnowledgeStatus.approved),
            (2, KnowledgeStatus.approved),
            (3, KnowledgeStatus.draft),
        ):
            body = f"版本 {number} 内容"
            version = KnowledgeVersion(
                tenant_id=tenant.id,
                doc_id=doc.id,
                version=number,
                body=body,
                status=status,
                checksum=content_hash(body),
                approved_at=utcnow() if status != KnowledgeStatus.draft else None,
            )
            session.add(version)
            await session.flush()
            session.add(
                KnowledgeChunk(
                    tenant_id=tenant.id,
                    version_id=version.id,
                    ordinal=0,
                    text=f"版本 {number} 片段",
                    embedding=[],
                    meta={"title": doc.title},
                )
            )
            versions.append(version.id)
        await session.commit()
        return tenant.id, versions


@pytest.mark.integration
async def test_publish_switch_and_failure_keeps_previous(test_sessionmaker):
    tenant_id, (v1_id, v2_id, v3_id) = await seed_versions(test_sessionmaker)
    index = FakeVectorIndex()

    async with test_sessionmaker() as session:
        first = await publish_index(
            session, index, tenant_id, [v1_id], collection="kb_test"
        )
    assert first["published"] is True
    assert first["status"] == "active"
    assert first["document_count"] == 1
    first_snapshot_id = first["snapshot"]["snapshot_id"]
    first_collection = first["snapshot"]["collection"]
    assert index.collections[first_collection][0]["text"] == "版本 1 片段"

    async with test_sessionmaker() as session:
        second = await publish_index(
            session, index, tenant_id, [v1_id, v2_id], collection="kb_test"
        )
    assert second["published"] is True
    assert second["snapshot"]["previous_snapshot_id"] == first_snapshot_id
    assert second["document_count"] == 2
    second_snapshot_id = second["snapshot"]["snapshot_id"]

    async with test_sessionmaker() as session:
        superseded = await session.get(IndexSnapshot, uuid.UUID(first_snapshot_id))
        assert superseded.status == "superseded"
        active = await active_snapshot(session, tenant_id)
        assert str(active.id) == second_snapshot_id

    failing = FakeVectorIndex(fail_on_build=True)
    async with test_sessionmaker() as session:
        failed = await publish_index(
            session, failing, tenant_id, [v2_id], collection="kb_test"
        )
    assert failed["published"] is False
    assert failed["status"] == "failed"
    assert failed["active_snapshot_id"] == second_snapshot_id
    assert failed["label"] == "索引发布失败，保留上一可用版本"
    assert failing.collections == {}

    async with test_sessionmaker() as session:
        active = await active_snapshot(session, tenant_id)
        assert str(active.id) == second_snapshot_id

    async with test_sessionmaker() as session:
        rolled = await rollback_index(session, tenant_id, first_snapshot_id)
    assert rolled["rolled_back"] is True
    assert rolled["snapshot"]["snapshot_id"] == first_snapshot_id
    assert rolled["previous_snapshot_id"] == second_snapshot_id

    async with test_sessionmaker() as session:
        snapshots = {
            str(snapshot.id): snapshot.status
            for snapshot in await session.scalars(
                select(IndexSnapshot).where(IndexSnapshot.tenant_id == tenant_id)
            )
        }
    assert snapshots[first_snapshot_id] == "active"
    assert snapshots[second_snapshot_id] == "superseded"
    assert failed["snapshot"]["status"] == "failed"

    async with test_sessionmaker() as session:
        with pytest.raises(IndexPublishError):
            await publish_index(session, index, tenant_id, [v3_id])


@pytest.mark.integration
async def test_rollback_rejects_foreign_or_unknown_snapshot(test_sessionmaker):
    tenant_id, (v1_id, _, _) = await seed_versions(test_sessionmaker)
    index = FakeVectorIndex()
    async with test_sessionmaker() as session:
        published = await publish_index(session, index, tenant_id, [v1_id])
    snapshot_id = published["snapshot"]["snapshot_id"]

    other_tenant_id, _ = await seed_versions(test_sessionmaker)
    async with test_sessionmaker() as session:
        with pytest.raises(IndexPublishError):
            await rollback_index(session, other_tenant_id, snapshot_id)
        with pytest.raises(IndexPublishError):
            await rollback_index(session, tenant_id, uuid.uuid4())

    async with test_sessionmaker() as session:
        idempotent = await rollback_index(
            session, tenant_id, published["snapshot"]["snapshot_id"]
        )
    assert idempotent["rolled_back"] is False
    assert idempotent["snapshot"]["snapshot_id"] == snapshot_id


@pytest.mark.integration
async def test_milvus_falls_back_to_fake_index_when_disabled():
    settings = Settings(milvus_enabled=False, milvus_uri="")
    assert isinstance(build_vector_index(settings), FakeVectorIndex)

    milvus = MilvusVectorIndex(settings)
    with pytest.raises(NotConfigured):
        await milvus.build("kb", [{"text": "x"}])

    enabled = Settings(milvus_enabled=True, milvus_uri="http://milvus:19530", milvus_token="t")
    with pytest.raises(NotConfigured):
        await MilvusVectorIndex(enabled).build("kb", [{"text": "x"}])