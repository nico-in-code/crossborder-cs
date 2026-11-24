from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models import ChannelReceipt, SendStatus, SendTask
from app.trace import utcnow

from stage2_helpers import create_ticket, login_headers, process
from stage3_helpers import approve, run_send_tick, ticket_payload


@pytest.mark.integration
async def test_edit_after_approval_blocks_old_task(
    client, app_instance, seeded, inbound_sender, api_login
):
    ticket = await create_ticket(client, inbound_sender, "JP-STAGE3-EDIT")
    ticket_id = ticket["ticket_id"]
    headers = await login_headers(api_login, client)
    await process(client, headers, ticket_id)
    detail = await ticket_payload(client, ticket_id)
    draft = detail["latest_draft"]
    await approve(client, headers, ticket_id, draft["id"], draft["content_hash"])

    response = await client.post(
        f"/api/tickets/{ticket_id}/drafts",
        headers=headers,
        json={"body": "編集済みの回答文です。返金手続きをご案内します。", "note": "add refund steps"},
    )
    assert response.status_code == 200, response.text
    edited = response.json()
    assert edited["version"] == 2

    detail = await ticket_payload(client, ticket_id)
    assert detail["latest_draft"]["id"] == edited["draft_id"]
    assert detail["latest_draft"]["content_hash"] == edited["content_hash"]
    assert all(not review["is_current"] for review in detail["reviews"])
    assert detail["send_tasks"][0]["status"] == "cancelled"

    # Simulate a crash window where the stale task is still pending: the send
    # worker must re-validate and refuse it even though the row says pending.
    async with app_instance.state.sessionmaker() as session:
        task = await session.scalar(select(SendTask))
        task.status = SendStatus.pending
        task.next_retry_at = utcnow()
        await session.commit()

    results = await run_send_tick(app_instance)
    assert any(
        item.get("reason") in {"review_not_current", "draft_superseded"}
        for item in results
    )

    async with app_instance.state.sessionmaker() as session:
        task = await session.scalar(select(SendTask))
        assert task.status is SendStatus.cancelled
        receipt_count = len(
            list((await session.scalars(select(ChannelReceipt))).all())
        )
    assert receipt_count == 0
    assert app_instance.state.channel_adapter.send_calls == 0