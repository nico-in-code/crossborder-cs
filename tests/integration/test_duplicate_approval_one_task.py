from __future__ import annotations

import asyncio

import pytest

from stage2_helpers import create_ticket, login_headers, process
from stage3_helpers import approve, ticket_payload


@pytest.mark.integration
async def test_concurrent_duplicate_approval_creates_one_task(
    client, seeded, inbound_sender, api_login
):
    ticket = await create_ticket(client, inbound_sender, "JP-STAGE3-DUP")
    ticket_id = ticket["ticket_id"]
    headers = await login_headers(api_login, client)
    await process(client, headers, ticket_id)
    detail = await ticket_payload(client, ticket_id)
    draft = detail["latest_draft"]

    first, second = await asyncio.gather(
        approve(client, headers, ticket_id, draft["id"], draft["content_hash"]),
        approve(client, headers, ticket_id, draft["id"], draft["content_hash"]),
    )

    assert first["review_id"] == second["review_id"]
    assert first["send_task_id"] == second["send_task_id"]
    assert {first["idempotent"], second["idempotent"]} == {True, False}

    detail = await ticket_payload(client, ticket_id)
    current = [
        review
        for review in detail["reviews"]
        if review["is_current"] and review["decision"] == "approved"
    ]
    assert len(current) == 1
    assert len(detail["send_tasks"]) == 1
    assert detail["send_tasks"][0]["review_id"] == current[0]["id"]