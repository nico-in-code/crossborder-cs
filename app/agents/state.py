from __future__ import annotations

from typing import TypedDict


class AgentState(TypedDict, total=False):
    ticket_id: str
    store_id: str
    original_text: str
    translated_text: str
    language: str
    order_candidates: list[str]
    classification_raw: dict
    intents: list[dict]
    entities: dict
    risk_flags: list[str]
    requires_human_review: bool
    route: str
    faq: dict | None
    evidence: list[dict]
    gaps: list[str]
    business: dict
    tool_outcomes: list[dict]