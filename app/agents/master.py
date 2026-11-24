from __future__ import annotations

from dataclasses import dataclass

from langgraph.graph import END, StateGraph
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.adapters.embed import EmbeddingAdapter
from app.adapters.llm import ClassificationError, LLMAdapter, validate_classification
from app.adapters.rerank import RerankerAdapter
from app.adapters.translate import Translator
from app.agents.business import AgentRuntime, run_business_queries
from app.agents.state import AgentState
from app.rag.evidence import retrieve_evidence
from app.rag.filters import RetrievalScope, load_scoped_faqs

BUSINESS_INTENTS = {"not_received", "refund_request", "shipping_status"}


@dataclass
class AgentAdapters:
    llm: LLMAdapter
    translator: Translator
    embedder: EmbeddingAdapter
    reranker: RerankerAdapter


def build_master_graph(
    adapters: AgentAdapters,
    runtime: AgentRuntime,
    sessionmaker: async_sessionmaker[AsyncSession],
):
    async def translate(state: AgentState) -> dict:
        gaps = list(state.get("gaps", []))
        try:
            translated = await adapters.translator.translate(
                state["original_text"], state.get("language", "ja"), "zh"
            )
        except Exception as exc:  # noqa: BLE001 - degrade to original text
            translated = state["original_text"]
            gaps.append(f"翻译失败（{type(exc).__name__}），保留原文并人工翻译")
        return {"translated_text": translated, "language": state.get("language", "ja"), "gaps": gaps}

    async def classify(state: AgentState) -> dict:
        raw = await adapters.llm.classify(
            state["original_text"],
            state.get("language", "ja"),
            list(state.get("order_candidates", [])),
        )
        return {"classification_raw": raw}

    async def validate(state: AgentState) -> dict:
        raw = state.get("classification_raw") or {}
        try:
            classification = validate_classification(raw)
        except ClassificationError as exc:
            raise ClassificationError(f"classification rejected: {exc}") from exc
        return {
            "intents": classification.intents,
            "entities": classification.entities,
            "risk_flags": classification.risk_flags,
            "requires_human_review": classification.requires_human_review,
            "language": classification.language,
        }

    def route(state: AgentState) -> str:
        codes = {intent["code"] for intent in state.get("intents", [])}
        if codes & BUSINESS_INTENTS:
            return "business"
        if "faq_policy" in codes:
            return "faq"
        return "human"

    async def business_query(state: AgentState) -> dict:
        result = await run_business_queries(
            runtime, list(state.get("order_candidates", []))
        )
        gaps = list(state.get("gaps", [])) + result["gaps"]
        return {
            "business": result["business"],
            "tool_outcomes": result["tool_outcomes"],
            "gaps": gaps,
            "route": "business",
        }

    async def faq_lookup(state: AgentState) -> dict:
        gaps = list(state.get("gaps", []))
        async with sessionmaker() as session:
            faqs = await load_scoped_faqs(
                session,
                RetrievalScope(
                    tenant_id=runtime.tenant_id,
                    store_ids={runtime.store_id},
                    marketplace="JP",
                    language="zh",
                ),
            )
        query = state.get("translated_text", state["original_text"])
        best = None
        best_score = 0.0
        for faq in faqs:
            score = await adapters.reranker.score(query, faq.question + faq.answer)
            if score > best_score:
                best = faq
                best_score = score
        if best is not None and best_score >= 0.2:
            return {
                "faq": {
                    "faq_id": str(best.id),
                    "question": best.question,
                    "answer": best.answer,
                    "score": round(best_score, 4),
                },
                "route": "faq",
                "gaps": gaps,
            }
        gaps.append("未命中已审核 FAQ，转人工处理")
        return {"faq": None, "route": "human", "gaps": gaps}

    async def human_review(state: AgentState) -> dict:
        gaps = list(state.get("gaps", []))
        gaps.append("分流结果为人工处理，草稿仅供辅助")
        return {"route": "human", "gaps": gaps}

    async def policy_search(state: AgentState) -> dict:
        gaps = list(state.get("gaps", []))
        business = state.get("business") or {}
        order = business.get("order") or {}
        items = order.get("items") or []
        product_scope = None
        if items and isinstance(items[0], dict) and items[0].get("sku"):
            product_scope = str(items[0]["sku"])
        query = " ".join(
            filter(
                None,
                [
                    state.get("translated_text", ""),
                    " ".join(
                        intent.get("label", "")
                        for intent in state.get("intents", [])
                    ),
                    "退款 未收到 物流 政策",
                ],
            )
        )
        async with sessionmaker() as session:
            evidence = await retrieve_evidence(
                session,
                RetrievalScope(
                    tenant_id=runtime.tenant_id,
                    store_ids={runtime.store_id},
                    marketplace="JP",
                    language="zh",
                    product_scope=product_scope,
                ),
                query,
                adapters.embedder,
                adapters.reranker,
                top_k=5,
            )
        if not evidence:
            gaps.append("日本站未检索到适用政策，需人工补充政策依据")
        return {
            "evidence": [item.to_dict() for item in evidence],
            "gaps": gaps,
        }

    graph = StateGraph(AgentState)
    graph.add_node("translate", translate)
    graph.add_node("classify", classify)
    graph.add_node("validate", validate)
    graph.add_node("business_query", business_query)
    graph.add_node("faq_lookup", faq_lookup)
    graph.add_node("human_review", human_review)
    graph.add_node("policy_search", policy_search)

    graph.set_entry_point("translate")
    graph.add_edge("translate", "classify")
    graph.add_edge("classify", "validate")
    graph.add_conditional_edges(
        "validate",
        route,
        {
            "business": "business_query",
            "faq": "faq_lookup",
            "human": "human_review",
        },
    )
    graph.add_edge("business_query", "policy_search")
    graph.add_edge("faq_lookup", "policy_search")
    graph.add_edge("human_review", "policy_search")
    graph.add_edge("policy_search", END)
    return graph.compile()