from __future__ import annotations

import pytest

from stage2_helpers import create_ticket, login_headers, process
from stage3_helpers import approve, flaky_channel, run_single_send, ticket_payload


@pytest.mark.integration
async def test_channel_rejection_is_failed_not_unknown(
    client, app_instance, seeded, inbound_sender, api_login
):
    ticket = await create_ticket(client, inbound_sender, "JP-STAGE3-REJECT")
    ticket_id = ticket["ticket_id"]
    headers = await login_headers(api_login, client)
    await process(client, headers, ticket_id)
    detail = await ticket_payload(client, ticket_id)
    draft = detail["latest_draft"]
    await approve(client, headers, ticket_id, draft["id"], draft["content_hash"])

    channel = flaky_channel(app_instance, mode="reject")
    result = await run_single_send(app_instance, channel)
    assert result["status"] == "failed"
    assert channel.send_calls == 1

    detail = await ticket_payload(client, ticket_id)
    assert detail["send_tasks"][0]["status"] == "failed"
    assert detail["send_tasks"][0]["last_error"] == "channel_rejected"
    assert detail["send_attempts"][0]["outcome"] == "failed"
    assert detail["send_attempts"][0]["response_code"] == "rejected"
    assert detail["channel_receipts"] == []
    assert detail["followups"] == []
    assert detail["ticket"]["status"] == "awaiting_review"