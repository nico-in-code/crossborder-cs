from __future__ import annotations

from httpx import AsyncClient

SAMPLE_TEXT = (
    "配送状況では配達済みになっていますが、荷物が届いていません。"
    "返金を申請したいです。"
)


async def create_ticket(
    client: AsyncClient,
    inbound_sender,
    external_id: str,
    *,
    account: str = "a-jp-01-amazon",
    buyer: str = "buyer-jp-001",
    order: str = "A-JP-1001",
) -> dict:
    response = await inbound_sender(
        client,
        account,
        {
            "external_message_id": external_id,
            "buyer_external_id": buyer,
            "buyer_display_name": "山田 花子",
            "text": SAMPLE_TEXT,
            "order_candidates": [order],
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


async def login_headers(api_login, client: AsyncClient, email: str = "cs.a@example.com") -> dict:
    payload = await api_login(client, email)
    return {"X-CSRF-Token": payload["csrf_token"]}


async def process(client: AsyncClient, headers: dict, ticket_id: str) -> dict:
    response = await client.post(
        f"/api/tickets/{ticket_id}/process", headers=headers
    )
    assert response.status_code == 200, response.text
    return response.json()