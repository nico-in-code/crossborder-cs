from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.vector_index import VectorIndex
from app.models import (
    IndexSnapshot,
    KnowledgeChunk,
    KnowledgeDoc,
    KnowledgeStatus,
    KnowledgeVersion,
)
from app.trace import utcnow

APPROVED_STATUSES = {KnowledgeStatus.approved, KnowledgeStatus.published}


class IndexPublishError(RuntimeError):
    pass


async def active_snapshot(
    session: AsyncSession, tenant_id: uuid.UUID
) -> IndexSnapshot | None:
    return await session.scalar(
        select(IndexSnapshot)
        .where(
            IndexSnapshot.tenant_id == tenant_id,
            IndexSnapshot.status == "active",
        )
        .order_by(IndexSnapshot.activated_at.desc().nullslast())
        .limit(1)
    )


async def recent_snapshots(
    session: AsyncSession, tenant_id: uuid.UUID, limit: int = 10
) -> list[IndexSnapshot]:
    return list(
        await session.scalars(
            select(IndexSnapshot)
            .where(IndexSnapshot.tenant_id == tenant_id)
            .order_by(IndexSnapshot.created_at.desc())
            .limit(limit)
        )
    )


async def approved_versions(
    session: AsyncSession, tenant_id: uuid.UUID
) -> list[dict]:
    rows = (
        await session.execute(
            select(KnowledgeVersion, KnowledgeDoc)
            .join(KnowledgeDoc, KnowledgeVersion.doc_id == KnowledgeDoc.id)
            .where(
                KnowledgeVersion.tenant_id == tenant_id,
                KnowledgeVersion.status.in_(APPROVED_STATUSES),
            )
            .order_by(KnowledgeDoc.title, KnowledgeVersion.version.desc())
        )
    ).all()
    return [
        {
            "id": str(version.id),
            "doc_id": str(doc.id),
            "doc_title": doc.title,
            "version": version.version,
            "status": version.status.value,
            "marketplace": doc.marketplace,
            "language": doc.language,
        }
        for version, doc in rows
    ]


async def _load_versions(
    session: AsyncSession, tenant_id: uuid.UUID, version_ids: list[uuid.UUID]
) -> list[KnowledgeVersion]:
    if not version_ids:
        raise IndexPublishError("at least one knowledge version is required")
    versions = list(
        await session.scalars(
            select(KnowledgeVersion).where(
                KnowledgeVersion.tenant_id == tenant_id,
                KnowledgeVersion.id.in_(version_ids),
            )
        )
    )
    found = {version.id for version in versions}
    missing = [str(version_id) for version_id in version_ids if version_id not in found]
    if missing:
        raise IndexPublishError(f"knowledge versions not found: {', '.join(missing)}")
    unapproved = [
        str(version.id)
        for version in versions
        if version.status not in APPROVED_STATUSES
    ]
    if unapproved:
        raise IndexPublishError(
            f"knowledge versions are not approved: {', '.join(unapproved)}"
        )
    return sorted(versions, key=lambda version: (str(version.doc_id), version.version))


async def _documents(
    session: AsyncSession, versions: list[KnowledgeVersion]
) -> list[dict]:
    version_ids = [version.id for version in versions]
    chunks = list(
        await session.scalars(
            select(KnowledgeChunk)
            .where(KnowledgeChunk.version_id.in_(version_ids))
            .order_by(KnowledgeChunk.version_id, KnowledgeChunk.ordinal)
        )
    )
    by_version = {version.id: version for version in versions}
    return [
        {
            "id": str(chunk.id),
            "text": chunk.text,
            "version_id": str(chunk.version_id),
            "doc_id": str(by_version[chunk.version_id].doc_id),
            "version": by_version[chunk.version_id].version,
            "checksum": by_version[chunk.version_id].checksum,
            "meta": chunk.meta,
        }
        for chunk in chunks
        if chunk.version_id in by_version
    ]


def _summary(snapshot: IndexSnapshot) -> dict:
    return {
        "snapshot_id": str(snapshot.id),
        "status": snapshot.status,
        "collection": snapshot.collection,
        "version_ids": list(snapshot.version_ids or []),
        "previous_snapshot_id": (
            str(snapshot.previous_snapshot_id) if snapshot.previous_snapshot_id else None
        ),
        "activated_at": snapshot.activated_at.isoformat() if snapshot.activated_at else None,
        "error": snapshot.error,
    }


async def publish_index(
    session: AsyncSession,
    index: VectorIndex,
    tenant_id: uuid.UUID,
    version_ids: list[uuid.UUID | str],
    *,
    collection: str = "knowledge_chunks",
) -> dict:
    normalized_ids = [uuid.UUID(str(version_id)) for version_id in version_ids]
    versions = await _load_versions(session, tenant_id, normalized_ids)
    documents = await _documents(session, versions)
    previous = await active_snapshot(session, tenant_id)

    snapshot_id = uuid.uuid4()
    snapshot = IndexSnapshot(
        id=snapshot_id,
        tenant_id=tenant_id,
        status="building",
        version_ids=[str(version.id) for version in versions],
        collection=f"{collection}__{snapshot_id.hex[:12]}",
        previous_snapshot_id=previous.id if previous is not None else None,
    )
    session.add(snapshot)
    await session.flush()

    try:
        result = await index.build(snapshot.collection, documents)
    except Exception as exc:  # noqa: BLE001 - snapshot records any build failure
        snapshot.status = "failed"
        snapshot.error = f"{type(exc).__name__}: {exc}"[:500]
        await session.commit()
        active = await active_snapshot(session, tenant_id)
        return {
            "published": False,
            "status": "failed",
            "snapshot": _summary(snapshot),
            "active_snapshot_id": str(active.id) if active is not None else None,
            "document_count": len(documents),
            "label": "索引发布失败，保留上一可用版本",
        }

    snapshot.status = "active"
    snapshot.activated_at = utcnow()
    snapshot.error = None
    if previous is not None and previous.id != snapshot.id:
        previous.status = "superseded"
    await session.commit()
    return {
        "published": True,
        "status": "active",
        "snapshot": _summary(snapshot),
        "active_snapshot_id": str(snapshot.id),
        "document_count": len(documents),
        "index_result": result,
        "label": "索引已切换",
    }


async def rollback_index(
    session: AsyncSession, tenant_id: uuid.UUID, snapshot_id: uuid.UUID | str
) -> dict:
    target = await session.get(IndexSnapshot, uuid.UUID(str(snapshot_id)))
    if target is None or target.tenant_id != tenant_id:
        raise IndexPublishError("index snapshot not found")
    current = await active_snapshot(session, tenant_id)
    if current is not None and current.id == target.id:
        return {
            "rolled_back": False,
            "status": "active",
            "snapshot": _summary(target),
            "label": "目标快照已是当前版本",
        }
    if target.status == "failed":
        raise IndexPublishError("cannot roll back to a failed snapshot")
    if target.status not in {"active", "superseded"}:
        raise IndexPublishError("snapshot is not rollback-able")

    target.status = "active"
    target.activated_at = utcnow()
    target.error = None
    if current is not None:
        current.status = "superseded"
    await session.commit()
    return {
        "rolled_back": True,
        "status": "active",
        "snapshot": _summary(target),
        "previous_snapshot_id": str(current.id) if current is not None else None,
        "label": "已回滚到上一索引快照",
    }