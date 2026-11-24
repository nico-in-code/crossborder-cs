from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models import ChannelReceipt, SendAttempt, SendOutcome, SendTask

from stage2_helpers import create_ticket, login_headers, process
from stage3_helpers import (
    approve,
    flaky_channel,
    run_send_tick,
    ticket_payload,
)


@pytest.mark.integration
async def test_channel_timeout_reconciles_without_resend(
    client, app_instance, seeded, inbound_sender, api_login
):
    ticket = await create_ticket(client, inbound_sender, "JP-STAGE3-FLAKY")
    ticket_id = ticket["ticket_id"]
    headers = await login_headers(api_login, client)
    await process(client, headers, ticket_id)
    detail = await ticket_payload(client, ticket_id)
    draft = detail["latest_draft"]
    await approve(client, headers, ticket_id, draft["id"], draft["content_hash"])

    channel = flaky_channel(app_instance)
    results = await run_send_tick(app_instance, channel)
    assert any(item["status"] == "reconciled_sent" for item in results)
    assert channel.send_calls == 1

    detail = await ticket_payload(client, ticket_id)
    assert detail["send_tasks"][0]["status"] == "reconciled_sent"
    assert len(detail["channel_receipts"]) == 1
    assert len(detail["send_attempts"]) == 1
    attempt = detail["send_attempts"][0]
    assert attempt["outcome"] == "channel_accepted"
    assert attempt["response_code"] == "reconciled"
    assert attempt["external_send_id"] == detail["channel_receipts"][0]["external_send_id"]
    assert attempt["finished_at"] is not None

    results = await run_send_tick(app_instance, channel)
    assert results == []
    assert channel.send_calls == 1

    async with app_instance.state.sessionmaker() as session:
        receipts = list((await session.scalars(select(ChannelReceipt))).all())
        tasks = list((await session.scalars(select(SendTask))).all())
        attempts = list((await session.scalars(select(SendAttempt))).all())
    assert len(receipts) == 1
    assert len({receipt.idempotency_key for receipt in receipts}) == 1
    assert len(tasks) == 1
    assert len(attempts) == 1
    assert attempts[0].outcome is SendOutcome.channel_accepted