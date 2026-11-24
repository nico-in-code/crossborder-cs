from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


INTENT_WHITELIST = {
    "not_received",
    "refund_request",
    "shipping_status",
    "faq_policy",
    "other",
}
RISK_WHITELIST = {"refund_request", "carrier_dispute", "high_value", "repeat_contact"}


class ClassificationError(ValueError):
    pass


@dataclass
class Classification:
    language: str
    intents: list[dict]
    entities: dict = field(default_factory=dict)
    risk_flags: list[str] = field(default_factory=list)
    requires_human_review: bool = False


class LLMAdapter(Protocol):
    async def classify(
        self, text: str, language: str, order_candidates: list[str]
    ) -> dict: ...


class DeterministicLLMAdapter:
    """Repeatable stand-in for an LLM classifier used in local verification."""

    async def classify(
        self, text: str, language: str, order_candidates: list[str]
    ) -> dict:
        intents: list[dict] = []
        lowered = text.lower()
        not_received = any(
            keyword in text
            for keyword in ("届いていません", "届いておりません", "受け取っていません", "未着")
        ) or "not received" in lowered
        refund = any(
            keyword in text
            for keyword in ("返金", "払い戻し", "返してほしい")
        ) or "refund" in lowered

        if not_received:
            intents.append(
                {
                    "code": "not_received",
                    "label": "未收到包裹",
                    "confidence": 0.96,
                }
            )
        if refund:
            intents.append(
                {
                    "code": "refund_request",
                    "label": "申请退款",
                    "confidence": 0.94,
                }
            )
        if not intents and ("配送" in text or "追跡" in text):
            intents.append(
                {"code": "shipping_status", "label": "查询物流状态", "confidence": 0.8}
            )
        if not intents:
            intents.append(
                {"code": "faq_policy", "label": "政策咨询", "confidence": 0.6}
            )

        risk_flags = ["refund_request"] if refund else []
        if not_received and refund:
            risk_flags.append("carrier_dispute")

        return {
            "language": language,
            "intents": intents,
            "entities": {
                "order_nos": list(order_candidates),
                "product_skus": [],
                "carrier": None,
            },
            "risk_flags": risk_flags,
            "requires_human_review": refund or not_received,
        }


def validate_classification(raw: dict) -> Classification:
    intents = raw.get("intents")
    if not isinstance(intents, list) or not intents:
        raise ClassificationError("classification must contain at least one intent")

    normalized_intents: list[dict] = []
    seen: set[str] = set()
    for item in intents:
        if not isinstance(item, dict):
            raise ClassificationError("intent must be an object")
        code = item.get("code")
        if code not in INTENT_WHITELIST:
            raise ClassificationError(f"intent code is not allowed: {code}")
        if code in seen:
            continue
        seen.add(code)
        normalized_intents.append(
            {
                "code": code,
                "label": str(item.get("label") or code),
                "confidence": float(item.get("confidence", 0.0)),
            }
        )

    entities = raw.get("entities") or {}
    if not isinstance(entities, dict):
        raise ClassificationError("entities must be an object")
    order_nos = entities.get("order_nos") or []
    product_skus = entities.get("product_skus") or []
    if not isinstance(order_nos, list) or not all(
        isinstance(value, str) for value in order_nos
    ):
        raise ClassificationError("entities.order_nos must be a list of strings")
    if not isinstance(product_skus, list) or not all(
        isinstance(value, str) for value in product_skus
    ):
        raise ClassificationError("entities.product_skus must be a list of strings")

    risk_flags = raw.get("risk_flags") or []
    if not isinstance(risk_flags, list) or not all(
        isinstance(flag, str) for flag in risk_flags
    ):
        raise ClassificationError("risk_flags must be a list of strings")
    unknown_flags = set(risk_flags) - RISK_WHITELIST
    if unknown_flags:
        raise ClassificationError(f"risk flags are not allowed: {sorted(unknown_flags)}")

    codes = {intent["code"] for intent in normalized_intents}
    requires_review = bool(raw.get("requires_human_review")) or bool(
        codes & {"refund_request", "not_received"}
    )
    if "refund_request" in codes and "refund_request" not in risk_flags:
        risk_flags.append("refund_request")

    return Classification(
        language=str(raw.get("language") or "ja"),
        intents=normalized_intents,
        entities={
            "order_nos": order_nos,
            "product_skus": product_skus,
            "carrier": entities.get("carrier"),
        },
        risk_flags=risk_flags,
        requires_human_review=requires_review,
    )