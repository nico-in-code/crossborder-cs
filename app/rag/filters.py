from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Faq,
    KnowledgeChunk,
    KnowledgeDoc,
    KnowledgeStatus,
    KnowledgeVersion,
)


@dataclass
class RetrievalScope:
    tenant_id: uuid.UUID
    store_ids: set[uuid.UUID]
    marketplace: str = "JP"
    language: str = "zh"
    product_scope: str | None = None
    now: datetime | None = None


def _scope_conditions(scope: RetrievalScope, now: datetime) -> list:
    conditions = [
        KnowledgeDoc.tenant_id == scope.tenant_id,
        or_(
            KnowledgeDoc.store_id.in_(scope.store_ids),
            KnowledgeDoc.store_id.is_(None),
        ),
        KnowledgeDoc.marketplace == scope.marketplace,
        KnowledgeDoc.language == scope.language,
        KnowledgeDoc.status.in_(
            [KnowledgeStatus.approved, KnowledgeStatus.published]
        ),
        KnowledgeVersion.status.in_(
            [KnowledgeStatus.approved, KnowledgeStatus.published]
        ),
        KnowledgeDoc.current_version_id == KnowledgeVersion.id,
        KnowledgeVersion.effective_from.is_not(None),
        KnowledgeVersion.effective_from <= now,
        or_(
            KnowledgeVersion.effective_to.is_(None),
            KnowledgeVersion.effective_to > now,
        ),
    ]
    if scope.product_scope:
        conditions.append(
            or_(
                KnowledgeDoc.product_scope == "*",
                KnowledgeDoc.product_scope == scope.product_scope,
            )
        )
    else:
        conditions.append(KnowledgeDoc.product_scope == "*")
    return conditions


async def load_scoped_chunks(
    session: AsyncSession, scope: RetrievalScope
) -> list[dict]:
    now = scope.now or datetime.now().astimezone()
    rows = (
        await session.execute(
            select(KnowledgeChunk, KnowledgeVersion, KnowledgeDoc)
            .join(KnowledgeVersion, KnowledgeChunk.version_id == KnowledgeVersion.id)
            .join(KnowledgeDoc, KnowledgeVersion.doc_id == KnowledgeDoc.id)
            .where(*_scope_conditions(scope, now))
            .order_by(KnowledgeDoc.id, KnowledgeChunk.ordinal)
        )
    ).all()
    return [
        {
            "chunk_id": str(chunk.id),
            "doc_id": str(doc.id),
            "doc_title": doc.title,
            "version": version.version,
            "version_id": str(version.id),
            "ordinal": chunk.ordinal,
            "text": chunk.text,
            "product_scope": doc.product_scope,
            "marketplace": doc.marketplace,
            "language": doc.language,
        }
        for chunk, version, doc in rows
    ]


async def load_scoped_faqs(
    session: AsyncSession, scope: RetrievalScope
) -> list[Faq]:
    now = scope.now or datetime.now().astimezone()
    conditions = [
        Faq.tenant_id == scope.tenant_id,
        or_(Faq.store_id.in_(scope.store_ids), Faq.store_id.is_(None)),
        Faq.marketplace == scope.marketplace,
        Faq.language == scope.language,
        Faq.status.in_([KnowledgeStatus.approved, KnowledgeStatus.published]),
        Faq.effective_from.is_not(None),
        Faq.effective_from <= now,
        or_(Faq.effective_to.is_(None), Faq.effective_to > now),
    ]
    if scope.product_scope:
        conditions.append(
            or_(Faq.product_scope == "*", Faq.product_scope == scope.product_scope)
        )
    else:
        conditions.append(Faq.product_scope == "*")
    return list((await session.scalars(select(Faq).where(*conditions))).all())