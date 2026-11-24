from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models import Draft, Message, Ticket, TranslationState

from stage2_helpers import SAMPLE_TEXT, create_ticket, login_headers, process


@pytest.mark.integration
async def test_two_intents_survive_translation_and_draft(
    client, seeded, inbound_sender, api_login, db
):
    ticket = await create_ticket(client, inbound_sender, "JP-STAGE2-INTS")
    headers = await login_headers(api_login, client)
    result = await process(client, headers, ticket["ticket_id"])

    codes = [intent["code"] for intent in result["intents"]]
    labels = [intent["label"] for intent in result["intents"]]
    assert codes == ["not_received", "refund_request"]
    assert labels == ["未收到包裹", "申请退款"]
    assert result["requires_human_review"] is True
    assert "refund_request" in result["risk_flags"]

    db.expire_all()
    message = await db.scalar(select(Message))
    assert message.body_text == SAMPLE_TEXT
    assert message.original_language == "ja"
    assert message.translation_state is TranslationState.done
    assert message.translated_language == "zh"
    assert "未收到" in message.translated_text
    assert "退款" in message.translated_text

    stored = await db.scalar(
        select(Ticket).where(Ticket.id == ticket["ticket_id"])
    )
    assert {intent["code"] for intent in stored.intents} == {
        "not_received",
        "refund_request",
    }
    draft = await db.scalar(
        select(Draft).where(Draft.ticket_id == stored.id).order_by(Draft.version.desc())
    )
    facts = draft.structured_facts
    assert {intent["code"] for intent in facts["buyer_claims"]["intents"]} == {
        "not_received",
        "refund_request",
    }
    assert facts["buyer_claims"]["original_text"] == SAMPLE_TEXT
    assert facts["buyer_claims"]["translated_text"] == result["translated_text"]
    assert "返金をご希望" in draft.body
    assert "荷物が届いていない" in draft.body