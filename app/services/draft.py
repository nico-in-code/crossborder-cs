from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Draft, Ticket
from app.security import content_hash


def _intent_codes(state: dict) -> set[str]:
    return {intent["code"] for intent in state.get("intents", [])}


def _verification_item(code: str, zh: str, ja: str) -> dict:
    return {"code": code, "zh": zh, "ja": ja}


def _gap_to_ja(gap: str) -> str:
    if "订单" in gap:
        return "注文情報の確認が完了していません。"
    if "运单" in gap or "運送" in gap or "物流" in gap or "照会" in gap:
        return "配送会社への照会が完了していません。"
    if "政策" in gap:
        return "適用ポリシーの確認が完了していません。"
    if "翻译" in gap:
        return "お問い合わせ内容の翻訳確認が必要です。"
    return "追加の確認が必要な事項があります。"


def _carrier_lines(business: dict) -> list[str]:
    shipment = business.get("shipment") or {}
    if not shipment:
        return ["・配送会社の記録は現在確認中です。"]
    lines = [
        f"・配送会社：{shipment.get('carrier') or '不明'}",
        f"・追跡番号：{shipment.get('tracking_no') or '不明'}",
        f"・配送状況：{shipment.get('delivery_status') or '不明'}",
    ]
    if shipment.get("delivered_at"):
        lines.append(f"・配達日時：{shipment['delivered_at']}")
    events = shipment.get("events") or []
    if events:
        latest = events[-1]
        detail = latest.get("detail") or latest.get("status")
        if detail:
            lines.append(f"・最新記録：{detail}")
    return lines


def build_structured_facts(state: dict) -> dict:
    business = state.get("business") or {}
    order = business.get("order") or {}
    shipment = business.get("shipment") or {}
    evidence = state.get("evidence") or []
    gaps = list(state.get("gaps", []))

    to_verify: list[dict] = []
    codes = _intent_codes(state)
    if "not_received" in codes:
        to_verify.append(
            _verification_item(
                "carrier_investigation",
                "向承运商确认妥投记录与签收信息",
                "配送会社へ配達記録と受領者情報の調査を依頼する",
            )
        )
    if "refund_request" in codes:
        to_verify.append(
            _verification_item(
                "refund_followup",
                "确认退款资格与金额，由具备退款权限的客服跟进",
                "返金の可否と金額を確認し、返金権限を持つ担当者がフォローする",
            )
        )
    if not order:
        to_verify.append(
            _verification_item(
                "order_linkage",
                "核对工单与订单归属",
                "ご注文とお問い合わせの紐付けを確認する",
            )
        )
    if any("超时" in gap or "タイムアウト" in gap for gap in gaps):
        to_verify.append(
            _verification_item(
                "tracking_recheck",
                "重新查询物流轨迹",
                "配送状況を再確認し、最新の追跡情報を取得する",
            )
        )
    if evidence:
        to_verify.append(
            _verification_item(
                "policy_compliance",
                "按适用政策执行，调查完成前不承诺退款",
                "適用ポリシーに従い、調査完了前に返金を確約しない",
            )
        )
    else:
        to_verify.append(
            _verification_item(
                "policy_gap",
                "确认并补充日本站适用政策",
                "日本站の適用ポリシーを確認・補完する",
            )
        )

    return {
        "buyer_claims": {
            "original_text": state.get("original_text"),
            "translated_text": state.get("translated_text"),
            "intents": state.get("intents", []),
        },
        "carrier_records": {
            "order_no": order.get("order_no"),
            "tracking_no": shipment.get("tracking_no"),
            "carrier": shipment.get("carrier"),
            "delivery_status": shipment.get("delivery_status"),
            "delivered_at": shipment.get("delivered_at"),
            "events": shipment.get("events") or [],
        },
        "inventory": business.get("inventory"),
        "faq": state.get("faq"),
        "to_verify": to_verify,
        "gaps": gaps,
        "policy_evidence": [
            {
                "chunk_id": item["chunk_id"],
                "doc_title": item["doc_title"],
                "version": item["version"],
                "text": item["text"],
                "rerank_score": item["rerank_score"],
            }
            for item in evidence
        ],
    }


def build_japanese_body(state: dict, facts: dict) -> str:
    codes = _intent_codes(state)
    buyer_lines: list[str] = []
    if "not_received" in codes:
        buyer_lines.append(
            "・物流情報では配達済みとなっておりますが、荷物が届いていないとのことです。"
        )
    if "refund_request" in codes:
        buyer_lines.append("・返金をご希望とのことです。")
    if not buyer_lines:
        buyer_lines.append("・お問い合わせ内容を確認いたしました。")

    carrier_lines = _carrier_lines(state.get("business") or {})
    verify_lines = [f"・{item['ja']}。" for item in facts["to_verify"]]
    gap_lines = []
    for gap in facts["gaps"]:
        line = _gap_to_ja(gap)
        if line not in gap_lines:
            gap_lines.append(line)
    gap_lines = [f"・{line}" for line in gap_lines]

    sections = [
        "お客様",
        "",
        "お問い合わせいただきありがとうございます。",
        "ご申告内容と配送状況を確認いたしました。",
        "",
        "【お客様のご申告】",
        *buyer_lines,
        "",
        "【配送会社の記録】",
        *carrier_lines,
        "",
        "【確認中の事項】",
        *verify_lines,
    ]
    if gap_lines:
        sections.extend(["", "【未確認事項】", *gap_lines])
    sections.extend(
        [
            "",
            "【ご案内】",
            "調査結果が分かり次第、改めてご連絡いたします。",
            "今しばらくお待ちいただけますようお願い申し上げます。",
            "",
            "カスタマーサポート",
        ]
    )
    return "\n".join(sections)


async def create_draft(
    session: AsyncSession,
    ticket: Ticket,
    state: dict,
    created_by_id: uuid.UUID | None = None,
) -> Draft:
    facts = build_structured_facts(state)
    body = build_japanese_body(state, facts)
    current_version = (
        await session.scalar(
            select(func.max(Draft.version)).where(Draft.ticket_id == ticket.id)
        )
        or 0
    )
    draft = Draft(
        tenant_id=ticket.tenant_id,
        ticket_id=ticket.id,
        version=current_version + 1,
        language="ja",
        body=body,
        content_hash=content_hash(body),
        structured_facts=facts,
        evidence=state.get("evidence", []),
        status="ready",
        created_by_id=created_by_id,
    )
    session.add(draft)
    await session.flush()
    return draft