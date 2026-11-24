from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app  # noqa: F401  (sets Windows selector loop policy)
from sqlalchemy import select

from app.adapters.embed import HashEmbeddingAdapter
from app.adapters.rerank import LexicalReranker
from app.config import get_settings
from app.db import build_engine, build_sessionmaker
from app.models import KnowledgeDoc, Store, Tenant
from app.rag.evidence import retrieve_evidence
from app.rag.filters import RetrievalScope

ROOT = Path(__file__).resolve().parents[1]

CASES = [
    {
        "tenant": "A",
        "store": "A-JP-01",
        "query": "物流显示妥投但买家未收到包裹怎么办",
        "expected": "日本站未收货与退款政策",
    },
    {
        "tenant": "A",
        "store": "A-JP-01",
        "query": "日本站退款政策是什么",
        "expected": "日本站未收货与退款政策",
    },
    {
        "tenant": "A",
        "store": "A-JP-01",
        "query": "承运商调查完成前可以承诺退款吗",
        "expected": "日本站未收货与退款政策",
    },
    {
        "tenant": "B",
        "store": "B-JP-01",
        "query": "B 公司退款口径是什么",
        "expected": "B 租户专用退款政策",
    },
    {
        "tenant": "B",
        "store": "B-JP-01",
        "query": "B 公司专用退款流程",
        "expected": "B 租户专用退款政策",
    },
]


async def main() -> None:
    settings = get_settings()
    engine = build_engine(settings)
    sessionmaker = build_sessionmaker(engine)
    embedder = HashEmbeddingAdapter()
    reranker = LexicalReranker()

    results = []
    try:
        async with sessionmaker() as session:
            tenants = {
                tenant.code: tenant
                for tenant in await session.scalars(select(Tenant))
            }
            stores = {
                (store.code): store
                for store in await session.scalars(select(Store))
            }
            docs_by_tenant: dict[str, set[str]] = {}
            for doc in await session.scalars(select(KnowledgeDoc)):
                docs_by_tenant.setdefault(str(doc.tenant_id), set()).add(doc.title)

        for case in CASES:
            tenant = tenants[case["tenant"]]
            store = stores[case["store"]]
            forbidden = set().union(
                *[
                    titles
                    for tenant_id, titles in docs_by_tenant.items()
                    if tenant_id != str(tenant.id)
                ]
            )
            async with sessionmaker() as session:
                evidence = await retrieve_evidence(
                    session,
                    RetrievalScope(
                        tenant_id=tenant.id,
                        store_ids={store.id},
                        marketplace="JP",
                        language="zh",
                    ),
                    case["query"],
                    embedder,
                    reranker,
                    top_k=3,
                )
            titles = [item.doc_title for item in evidence]
            rank = next(
                (
                    index
                    for index, title in enumerate(titles, start=1)
                    if title == case["expected"]
                ),
                0,
            )
            leaks = sorted(set(titles) & forbidden)
            results.append(
                {
                    **case,
                    "titles": titles,
                    "rank": rank,
                    "hit_at_3": rank > 0,
                    "mrr": round(1.0 / rank, 4) if rank else 0.0,
                    "leaks": leaks,
                    "evidence": [item.to_dict() for item in evidence],
                }
            )
    finally:
        await engine.dispose()

    hits = sum(1 for result in results if result["hit_at_3"])
    mrr = sum(result["mrr"] for result in results) / len(results)
    leaks = sum(len(result["leaks"]) for result in results)
    summary = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "cases": len(results),
        "hit_at_3": f"{hits}/{len(results)}",
        "mrr": round(mrr, 4),
        "cross_tenant_leaks": leaks,
        "results": results,
    }

    artifact = ROOT / "artifacts" / "retrieval-eval.json"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    report = ROOT / "docs" / "retrieval-eval.md"
    lines = [
        "# 检索评估报告",
        "",
        f"生成时间：{summary['generated_at']}",
        "",
        "检索链路：作用域过滤 → BM25 + 哈希向量召回 → RRF 融合 → 词面重排。"
        "语言为中文（买家日语原文在入库时翻译为中文后参与检索）。",
        "",
        "| # | 租户/店铺 | 查询 | 期望文档 | 命中@3 | 排名 | 跨租户越权 |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for index, result in enumerate(results, start=1):
        lines.append(
            f"| {index} | {result['tenant']}/{result['store']} | {result['query']} | "
            f"{result['expected']} | {'是' if result['hit_at_3'] else '否'} | "
            f"{result['rank'] or '-'} | {len(result['leaks'])} |"
        )
    lines.extend(
        [
            "",
            "## 汇总",
            "",
            f"- 命中@3：{summary['hit_at_3']}",
            f"- MRR：{summary['mrr']}",
            f"- 跨租户越权命中：{summary['cross_tenant_leaks']}",
            "",
            "跨店铺作用域、版本有效期与商品范围过滤由 "
            "`tests/integration/test_rag_filters.py` 覆盖；本报告聚焦真实种子数据上的"
            "中文查询命中与租户隔离。",
        ]
    )
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({k: summary[k] for k in ("cases", "hit_at_3", "mrr", "cross_tenant_leaks")}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())