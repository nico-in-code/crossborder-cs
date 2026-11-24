from __future__ import annotations

import pytest

from stage2_helpers import create_ticket, login_headers, process
from stage3_helpers import (
    approve,
    flaky_channel,
    run_send_tick,
    run_single_send,
    ticket_payload,
)

CONCLUSION = "承运商确认地址异常，已提交退款申请并告知买家。"
BASIS = "承运商轨迹：投递地址与订单地址不一致；退款工单 R-2026-001。"


async def _prepare(
    client, inbound_sender, api_login, external_id: str
) -> tuple[str, dict]:
    ticket = await create_ticket(client, inbound_sender, external_id)
    headers = await login_headers(api_login, client)
    await process(client, headers, ticket["ticket_id"])
    detail = await ticket_payload(client, ticket["ticket_id"])
    draft = detail["latest_draft"]
    await approve(
        client, headers, ticket["ticket_id"], draft["id"], draft["content_hash"]
    )
    return ticket["ticket_id"], headers


@pytest.mark.integration
async def test_send_success_keeps_ticket_open_until_notice(
    client, app_instance, seeded, inbound_sender, api_login
):
    ticket_id, headers = await _prepare(
        client, inbound_sender, api_login, "JP-STAGE3-OPEN"
    )

    results = await run_send_tick(app_instance)
    assert results[0]["status"] == "acked"

    detail = await ticket_payload(client, ticket_id)
    assert detail["ticket"]["status"] == "open"
    assert detail["send_tasks"][0]["status"] == "acked"
    followups = [item for item in detail["followups"] if item["kind"] == "refund"]
    assert len(followups) == 1
    assert followups[0]["status"] == "pending"

    response = await client.post(
        f"/api/tickets/{ticket_id}/close",
        headers=headers,
        json={"conclusion": CONCLUSION, "basis": BASIS, "notified": False},
    )
    assert response.status_code == 409, response.text

    response = await client.post(
        f"/api/tickets/{ticket_id}/close",
        headers=headers,
        json={"conclusion": CONCLUSION, "basis": BASIS, "notified": True},
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "closed"

    detail = await ticket_payload(client, ticket_id)
    assert detail["ticket"]["status"] == "closed"
    assert detail["followups"][0]["status"] == "notified"
    assert detail["followups"][0]["notified_at"] is not None


@pytest.mark.integration
async def test_unknown_send_blocks_close_until_reconciled(
    client, app_instance, seeded, inbound_sender, api_login
):
    ticket_id, headers = await _prepare(
        client, inbound_sender, api_login, "JP-STAGE3-UNKNOWN"
    )

    channel = flaky_channel(app_instance)
    result = await run_single_send(app_instance, channel)
    assert result["status"] == "unknown"
    assert channel.send_calls == 1

    response = await client.post(
        f"/api/tickets/{ticket_id}/close",
        headers=headers,
        json={"conclusion": CONCLUSION, "basis": BASIS, "notified": True},
    )
    assert response.status_code == 409, response.text
    assert "send" in response.json()["detail"]

    await run_send_tick(app_instance, channel)
    detail = await ticket_payload(client, ticket_id)
    assert detail["send_tasks"][0]["status"] == "reconciled_sent"
    assert len(detail["channel_receipts"]) == 1

    response = await client.post(
        f"/api/tickets/{ticket_id}/close",
        headers=headers,
        json={"conclusion": CONCLUSION, "basis": BASIS, "notified": True},
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "closed"