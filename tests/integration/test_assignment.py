from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models import Employee

from stage2_helpers import create_ticket, login_headers
from stage3_helpers import ticket_payload


@pytest.mark.integration
async def test_only_supervisor_assigns_within_tenant(
    client, seeded, inbound_sender, api_login, db
):
    ticket = await create_ticket(client, inbound_sender, "JP-STAGE3-ASSIGN")
    ticket_id = ticket["ticket_id"]
    cs_a = await db.scalar(
        select(Employee).where(Employee.email == "cs.a@example.com")
    )
    cs_b = await db.scalar(
        select(Employee).where(Employee.email == "cs.b@example.com")
    )

    agent_headers = await login_headers(api_login, client, "cs.a@example.com")
    response = await client.post(
        f"/api/tickets/{ticket_id}/assign",
        headers=agent_headers,
        json={"assignee_id": str(cs_a.id)},
    )
    assert response.status_code == 403, response.text

    supervisor_headers = await login_headers(
        api_login, client, "sup.a@example.com"
    )
    response = await client.post(
        f"/api/tickets/{ticket_id}/assign",
        headers=supervisor_headers,
        json={"assignee_id": str(cs_b.id)},
    )
    assert response.status_code == 404, response.text

    response = await client.post(
        f"/api/tickets/{ticket_id}/assign",
        headers=supervisor_headers,
        json={"assignee_id": str(cs_a.id)},
    )
    assert response.status_code == 200, response.text
    detail = await ticket_payload(client, ticket_id)
    assert detail["assignee"]["id"] == str(cs_a.id)
    assert detail["assignee"]["display_name"] == cs_a.display_name