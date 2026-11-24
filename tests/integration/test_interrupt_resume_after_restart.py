from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import create_app

from stage2_helpers import create_ticket, login_headers, process
from stage3_helpers import approve, build_test_settings, ticket_payload


@pytest.mark.integration
async def test_review_interrupt_survives_app_restart(
    settings, seeded, inbound_sender, api_login
):
    cfg = build_test_settings(settings)
    ticket_id: str
    draft: dict

    app1 = create_app(cfg)
    async with app1.router.lifespan_context(app1):
        transport = ASGITransport(app=app1)
        async with AsyncClient(
            transport=transport, base_url="http://testserver", timeout=30
        ) as client1:
            ticket = await create_ticket(
                client1, inbound_sender, "JP-STAGE3-RESTART"
            )
            ticket_id = ticket["ticket_id"]
            headers = await login_headers(api_login, client1)
            await process(client1, headers, ticket_id)
            detail = await ticket_payload(client1, ticket_id)
            assert detail["workflow"] is not None
            assert detail["workflow"]["next"] == ["await_review"]
            draft = detail["latest_draft"]
            assert draft is not None

    app2 = create_app(cfg)
    async with app2.router.lifespan_context(app2):
        transport = ASGITransport(app=app2)
        async with AsyncClient(
            transport=transport, base_url="http://testserver", timeout=30
        ) as client2:
            headers = await login_headers(api_login, client2)
            detail = await ticket_payload(client2, ticket_id)
            assert detail["workflow"] is not None
            assert detail["workflow"]["next"] == ["await_review"]

            result = await approve(
                client2,
                headers,
                ticket_id,
                draft["id"],
                draft["content_hash"],
                note="重启后恢复审核",
            )
            assert result["idempotent"] is False
            assert result["workflow"]["status"] == "reviewed"

            detail = await ticket_payload(client2, ticket_id)
            assert detail["workflow"]["values"]["status"] == "reviewed"
            assert detail["workflow"]["next"] == []
            current = [
                review
                for review in detail["reviews"]
                if review["is_current"] and review["decision"] == "approved"
            ]
            assert len(current) == 1
            assert current[0]["draft_content_hash"] == draft["content_hash"]
            assert len(detail["send_tasks"]) == 1
            assert detail["send_tasks"][0]["status"] == "pending"