from __future__ import annotations

import pytest

SAMPLE_TEXT = (
    "配送状況では配達済みになっていますが、荷物が届いていません。"
    "返金を申請したいです。"
)


async def _create_ticket(client, inbound_sender, account, external_id, buyer, order):
    response = await inbound_sender(
        client,
        account,
        {
            "external_message_id": external_id,
            "buyer_external_id": buyer,
            "text": SAMPLE_TEXT,
            "order_candidates": [order],
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["ticket_id"]


async def _seed_three_tickets(client, inbound_sender):
    a_ticket = await _create_ticket(
        client, inbound_sender, "a-jp-01-amazon", "JP-ISO-0001", "buyer-jp-001", "A-JP-1001"
    )
    a2_ticket = await _create_ticket(
        client, inbound_sender, "a-jp-02-amazon", "JP-ISO-0002", "buyer-jp-002", "A2-JP-2001"
    )
    b_ticket = await _create_ticket(
        client, inbound_sender, "b-jp-01-amazon", "JP-ISO-0003", "buyer-jp-101", "B-JP-3001"
    )
    return a_ticket, a2_ticket, b_ticket


@pytest.mark.integration
async def test_agent_only_sees_granted_store(
    client, seeded, inbound_sender, api_login
):
    a_ticket, a2_ticket, b_ticket = await _seed_three_tickets(client, inbound_sender)

    await api_login(client, "cs.a@example.com")

    listing = await client.get("/api/tickets")
    assert listing.status_code == 200
    items = listing.json()["items"]
    assert [item["store"]["code"] for item in items] == ["A-JP-01"]
    assert [item["id"] for item in items] == [a_ticket]

    allowed = await client.get(f"/api/tickets/{a_ticket}")
    assert allowed.status_code == 200
    assert allowed.json()["messages"][0]["body_text"] == SAMPLE_TEXT

    assert (await client.get(f"/api/tickets/{a2_ticket}")).status_code == 404
    assert (await client.get(f"/api/tickets/{b_ticket}")).status_code == 404


@pytest.mark.integration
async def test_other_tenant_agent_cannot_read_ticket(
    client, seeded, inbound_sender, api_login
):
    a_ticket, _a2_ticket, b_ticket = await _seed_three_tickets(client, inbound_sender)

    await api_login(client, "cs.b@example.com")
    listing = await client.get("/api/tickets")
    assert listing.status_code == 200
    assert [item["id"] for item in listing.json()["items"]] == [b_ticket]

    assert (await client.get(f"/api/tickets/{a_ticket}")).status_code == 404


@pytest.mark.integration
async def test_non_ticket_roles_have_no_ticket_access(
    client, seeded, inbound_sender, api_login
):
    a_ticket, _a2_ticket, _b_ticket = await _seed_three_tickets(client, inbound_sender)

    for email in ("kb.a@example.com", "admin.a@example.com"):
        await api_login(client, email)
        listing = await client.get("/api/tickets")
        assert listing.status_code == 200
        assert listing.json()["items"] == []
        detail = await client.get(f"/api/tickets/{a_ticket}")
        assert detail.status_code == 403, email


@pytest.mark.integration
async def test_supervisor_with_grant_can_read(
    client, seeded, inbound_sender, api_login
):
    a_ticket, _a2_ticket, _b_ticket = await _seed_three_tickets(client, inbound_sender)

    await api_login(client, "sup.a@example.com")
    detail = await client.get(f"/api/tickets/{a_ticket}")
    assert detail.status_code == 200
    assert detail.json()["ticket"]["id"] == a_ticket


@pytest.mark.integration
async def test_ticket_endpoints_require_authentication(client, seeded):
    assert (await client.get("/api/tickets")).status_code == 401