from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.adapters.embed import HashEmbeddingAdapter
from app.adapters.rerank import LexicalReranker
from app.models import (
    KnowledgeChunk,
    KnowledgeDoc,
    KnowledgeStatus,
    KnowledgeVersion,
    Store,
    Tenant,
)
from app.rag.evidence import retrieve_evidence
from app.rag.filters import RetrievalScope, load_scoped_faqs
from app.security import content_hash


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _insert_doc(
    session,
    *,
    tenant_id,
    store_id,
    title: str,
    body: str,
    language: str = "zh",
    product_scope: str = "*",
    doc_status: KnowledgeStatus = KnowledgeStatus.published,
    version_status: KnowledgeStatus = KnowledgeStatus.published,
    effective_from: datetime | None = None,
    effective_to: datetime | None = None,
    previous_version: str | None = None,
):
    doc = KnowledgeDoc(
        tenant_id=tenant_id,
        store_id=store_id,
        marketplace="JP",
        language=language,
        product_scope=product_scope,
        title=title,
        status=doc_status,
    )
    session.add(doc)
    await session.flush()

    if previous_version is not None:
        old = KnowledgeVersion(
            tenant_id=tenant_id,
            doc_id=doc.id,
            version=1,
            body=previous_version,
            status=KnowledgeStatus.approved,
            checksum=content_hash(previous_version),
            effective_from=_now() - timedelta(days=30),
        )
        session.add(old)
        await session.flush()
        session.add(
            KnowledgeChunk(
                tenant_id=tenant_id,
                version_id=old.id,
                ordinal=0,
                text=previous_version,
                embedding=[],
                meta={},
            )
        )
        version = KnowledgeVersion(
            tenant_id=tenant_id,
            doc_id=doc.id,
            version=2,
            body=body,
            status=version_status,
            checksum=content_hash(body),
            effective_from=effective_from or (_now() - timedelta(days=1)),
            effective_to=effective_to,
        )
        session.add(version)
        await session.flush()
    else:
        version = KnowledgeVersion(
            tenant_id=tenant_id,
            doc_id=doc.id,
            version=1,
            body=body,
            status=version_status,
            checksum=content_hash(body),
            effective_from=effective_from or (_now() - timedelta(days=1)),
            effective_to=effective_to,
        )
        session.add(version)
        await session.flush()

    doc.current_version_id = version.id
    for index, line in enumerate(
        [part.strip() for part in body.split("\n") if part.strip()]
    ):
        session.add(
            KnowledgeChunk(
                tenant_id=tenant_id,
                version_id=version.id,
                ordinal=index,
                text=line,
                embedding=[],
                meta={"title": title},
            )
        )
    return doc


async def _scope(db, tenant_code: str, store_code: str, **kwargs) -> RetrievalScope:
    tenant = await db.scalar(select(Tenant).where(Tenant.code == tenant_code))
    store = await db.scalar(select(Store).where(Store.code == store_code))
    return RetrievalScope(tenant_id=tenant.id, store_ids={store.id}, **kwargs)


@pytest.mark.integration
async def test_rag_scope_filters_and_ranking(db, seeded):
    tenant_a = await db.scalar(select(Tenant).where(Tenant.code == "A"))
    tenant_b = await db.scalar(select(Tenant).where(Tenant.code == "B"))
    store_a1 = await db.scalar(select(Store).where(Store.code == "A-JP-01"))
    store_a2 = await db.scalar(select(Store).where(Store.code == "A-JP-02"))
    store_b = await db.scalar(select(Store).where(Store.code == "B-JP-01"))

    strong = await _insert_doc(
        db,
        tenant_id=tenant_a.id,
        store_id=store_a1.id,
        title="A 日本站未收货退款政策",
        body=(
            "未收到包裹时先核对订单与物流轨迹。\n"
            "承运商显示妥投但买家未收到时发起调查。\n"
            "调查完成前不得承诺退款。"
        ),
    )
    weak = await _insert_doc(
        db,
        tenant_id=tenant_a.id,
        store_id=store_a1.id,
        title="A 日本站客服营业时间",
        body="客服营业时间与退款咨询窗口说明。",
    )
    await _insert_doc(
        db,
        tenant_id=tenant_a.id,
        store_id=store_a2.id,
        title="A 二店专属未收货政策",
        body="未收到包裹退款仅适用于二店。",
    )
    await _insert_doc(
        db,
        tenant_id=tenant_b.id,
        store_id=store_b.id,
        title="B 租户专用退款政策",
        body="未收到包裹退款仅适用于 B 租户。",
    )
    await _insert_doc(
        db,
        tenant_id=tenant_a.id,
        store_id=store_a1.id,
        title="日本站日文政策",
        body="未收到包裹时の返金対応。",
        language="ja",
    )
    other_product = await _insert_doc(
        db,
        tenant_id=tenant_a.id,
        store_id=store_a1.id,
        title="特定商品未收货政策",
        body="特定商品 SKU-OTHER 的未收到包裹退款流程。",
        product_scope="SKU-OTHER",
    )
    await _insert_doc(
        db,
        tenant_id=tenant_a.id,
        store_id=store_a1.id,
        title="已过期未收货政策",
        body="未收到包裹退款旧政策。",
        effective_from=_now() - timedelta(days=30),
        effective_to=_now() - timedelta(days=1),
    )
    await _insert_doc(
        db,
        tenant_id=tenant_a.id,
        store_id=store_a1.id,
        title="待发布新版未收货政策",
        body="未收到包裹退款新版政策草案。",
        doc_status=KnowledgeStatus.pending_review,
        version_status=KnowledgeStatus.pending_review,
        previous_version="未收到包裹退款旧版本。",
    )
    await db.commit()

    embedder = HashEmbeddingAdapter()
    reranker = LexicalReranker()
    query = "未收到包裹 退款 物流 政策"
    scope = await _scope(
        db, "A", "A-JP-01", language="zh", product_scope="SKU-JP-001"
    )
    results = await retrieve_evidence(db, scope, query, embedder, reranker, top_k=10)

    titles = [item.doc_title for item in results]
    assert set(titles) <= {
        "日本站未收货与退款政策",
        "A 日本站未收货退款政策",
        "A 日本站客服营业时间",
    }
    assert "A 二店专属未收货政策" not in titles
    assert "B 租户专用退款政策" not in titles
    assert "日本站日文政策" not in titles
    assert "特定商品未收货政策" not in titles
    assert "已过期未收货政策" not in titles
    assert "待发布新版未收货政策" not in titles
    assert all(item.version == 1 for item in results)
    assert "A 日本站未收货退款政策" in titles
    assert titles.index("A 日本站未收货退款政策") < titles.index("A 日本站客服营业时间")
    strong_item = next(
        item for item in results if item.doc_title == "A 日本站未收货退款政策"
    )
    weak_item = next(
        item for item in results if item.doc_title == "A 日本站客服营业时间"
    )
    assert strong_item.bm25_score > 0
    assert strong_item.rerank_score > weak_item.rerank_score
    assert results[0].rrf_score > 0

    product_results = await retrieve_evidence(
        db,
        await _scope(db, "A", "A-JP-01", language="zh", product_scope="SKU-OTHER"),
        query,
        embedder,
        reranker,
        top_k=10,
    )
    product_titles = [item.doc_title for item in product_results]
    assert "特定商品未收货政策" in product_titles
    assert "A 日本站未收货退款政策" in product_titles

    other_store_results = await retrieve_evidence(
        db,
        await _scope(db, "A", "A-JP-02", language="zh"),
        query,
        embedder,
        reranker,
        top_k=10,
    )
    other_store_titles = [item.doc_title for item in other_store_results]
    assert "A 二店专属未收货政策" in other_store_titles
    assert "A 日本站未收货退款政策" not in other_store_titles
    assert "特定商品未收货政策" not in other_store_titles

    faqs = await load_scoped_faqs(
        db, await _scope(db, "A", "A-JP-02", language="zh")
    )
    assert faqs == []
    faqs_b = await load_scoped_faqs(
        db, await _scope(db, "B", "B-JP-01", language="zh")
    )
    assert [faq.question for faq in faqs_b] == ["B 公司专用退款口径是什么？"]