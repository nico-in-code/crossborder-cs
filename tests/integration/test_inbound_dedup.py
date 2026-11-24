from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import func, select

from app.models import Message, Store, Tenant, Ticket

SAMPLE_TEXT = (
    "配送状況では配達済みになっていますが、荷物が届いていません。"
    "返金を申請したいです。"
)


def inbound_payload(external_id: str, buyer: str = "buyer-jp-001", **extra):
    payload = {
        "external_message_id": external_id,
        "buyer_external_id": buyer,
        "buyer_display_name": "山田 花子",
        "text": SAMPLE_TEXT,
        "order_candidates": ["A-JP-1001"],
    }
    payload.update(extra)
    return payload


@pytest.mark.integration
async def test_concurrent_identical_payload_creates_one_message_and_ticket(
    client, seeded, inbound_sender, db
):
    payload = inbound_payload("JP-DEDUP-0001")

    responses = await asyncio.gather(
        *[inbound_sender(client, "a-jp-01-amazon", payload) for _ in range(8)]
    )

    assert [response.status_code for response in responses] == [200] * 8
    bodies = [response.json() for response in responses]
    ticket_ids = {body["ticket_id"] for body in bodies}
    assert len(ticket_ids) == 1
    assert sum(1 for body in bodies if body["duplicate"] is False) == 1
    assert sum(1 for body in bodies if body["duplicate"] is True) == 7
    assert all(body["ticket_number"] for body in bodies)

    message_count = await db.scalar(
        select(func.count()).select_from(Message).where(
            Message.external_message_id == "JP-DEDUP-0001"
        )
    )
    ticket_count = await db.scalar(select(func.count()).select_from(Ticket))
    assert message_count == 1
    assert ticket_count == 1

    message = await db.scalar(select(Message))
    assert message.body_text == SAMPLE_TEXT
    assert message.original_language == "ja"
    assert str(message.ticket_id) == bodies[0]["ticket_id"]


@pytest.mark.integration
async def test_same_external_id_in_two_tenants_is_stored_separately(
    client, seeded, inbound_sender, db
):
    shared_external_id = "JP-SHARED-0001"
    response_a = await inbound_sender(
        client, "a-jp-01-amazon", inbound_payload(shared_external_id)
    )
    response_b = await inbound_sender(
        client,
        "b-jp-01-amazon",
        inbound_payload(
            shared_external_id,
            buyer="buyer-jp-101",
            order_candidates=["B-JP-3001"],
        ),
    )

    assert response_a.status_code == 200
    assert response_b.status_code == 200
    body_a, body_b = response_a.json(), response_b.json()
    assert body_a["ticket_id"] != body_b["ticket_id"]
    assert body_a["duplicate"] is False
    assert body_b["duplicate"] is False

    message_count = await db.scalar(
        select(func.count()).select_from(Message).where(
            Message.external_message_id == shared_external_id
        )
    )
    assert message_count == 2
    ticket_count = await db.scalar(select(func.count()).select_from(Ticket))
    assert ticket_count == 2


@pytest.mark.integration
async def test_replaying_same_payload_returns_same_ticket(
    client, seeded, inbound_sender
):
    payload = inbound_payload("JP-REPLAY-0001")
    first = await inbound_sender(client, "a-jp-01-amazon", payload)
    second = await inbound_sender(client, "a-jp-01-amazon", payload)

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["duplicate"] is True
    assert first.json()["ticket_id"] == second.json()["ticket_id"]
    assert first.json()["ticket_number"] == second.json()["ticket_number"]


@pytest.mark.integration
async def test_forged_scope_fields_in_body_are_ignored(
    client, seeded, inbound_sender, db
):
    tenant_a = await db.scalar(select(Tenant).where(Tenant.code == "A"))
    store_a = await db.scalar(select(Store).where(Store.code == "A-JP-01"))
    tenant_b = await db.scalar(select(Tenant).where(Tenant.code == "B"))
    store_b = await db.scalar(select(Store).where(Store.code == "B-JP-01"))

    payload = inbound_payload(
        "JP-FORGED-0001",
        tenant_id=str(tenant_b.id),
        store_id=str(store_b.id),
        employee_id=str(tenant_b.id),
    )
    response = await inbound_sender(client, "a-jp-01-amazon", payload)
    assert response.status_code == 200

    ticket = await db.scalar(select(Ticket))
    message = await db.scalar(select(Message))
    assert ticket.tenant_id == tenant_a.id
    assert ticket.store_id == store_a.id
    assert message.tenant_id == tenant_a.id
    assert message.store_id == store_a.id
    assert response.json()["ticket_id"] == str(ticket.id)


@pytest.mark.integration
async def test_inbound_rejects_bad_signature_and_unknown_channel(
    client, seeded, inbound_sender
):
    valid = await inbound_sender(client, "a-jp-01-amazon", inbound_payload("JP-BAD-0001"))
    # Same payload shape signed with a wrong secret.
    import json

    from app.security import sign_hmac

    body = json.dumps(inbound_payload("JP-BAD-0002"), ensure_ascii=False).encode()
    forged = await client.post(
        "/api/inbound/a-jp-01-amazon",
        content=body,
        headers={
            "content-type": "application/json",
            "X-Channel-Signature": sign_hmac("wrong-secret", body),
        },
    )
    unknown = await client.post(
        "/api/inbound/unknown-channel",
        content=body,
        headers={
            "content-type": "application/json",
            "X-Channel-Signature": sign_hmac("wrong-secret", body),
        },
    )

    assert valid.status_code == 200
    assert forged.status_code == 401
    assert unknown.status_code == 401
    assert forged.json()["detail"] == unknown.json()["detail"]