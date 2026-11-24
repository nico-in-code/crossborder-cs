from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select, update

from app.models import (
    AgentRun,
    Employee,
    Followup,
    Ticket,
    ToolCall,
    ToolCallStatus,
)

from stage2_helpers import create_ticket, login_headers, process


async def _diagnose(client, headers, ticket_id: str) -> dict:
    response = await client.post(
        f"/api/tickets/{ticket_id}/diagnose", headers=headers
    )
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.integration
async def test_diagnosis_reuse_and_apply_after_confirmation(
    client, seeded, inbound_sender, api_login, db
):
    ticket = await create_ticket(client, inbound_sender, "JP-STAGE4-REUSE")
    headers = await login_headers(api_login, client)
    await process(client, headers, ticket["ticket_id"])

    body = await _diagnose(client, headers, ticket["ticket_id"])
    assert body["path"] == "reuse"
    assert body["requires_manual_review"] is False
    assert body["evidence"]["sop"]["version"] == 1
    assert body["evidence"]["sop"]["body"]
    assert body["evidence"]["draft"]["version"] == 1
    assert len(body["evidence"]["messages"]) == 1
    tool_names = {item["tool_name"] for item in body["evidence"]["tool_calls"]}
    assert {"order_get", "shipments_list", "shipment_track"} <= tool_names
    assert all(item["source"] == "master" for item in body["evidence"]["tool_calls"])
    assert body["recommendation"]["next_step"]
    assert body["recommendation"]["next_owner"]["display_name"]
    assert body["recommendation"]["requires_manual_review"] is False

    db.expire_all()
    run = await db.get(AgentRun, uuid.UUID(body["diagnosis_id"]))
    assert run is not None and run.graph == "diagnose"
    reuse_calls = (
        await db.scalars(select(ToolCall).where(ToolCall.agent_run_id == run.id))
    ).all()
    assert reuse_calls == []

    agent = await db.scalar(
        select(Employee).where(Employee.email == "cs.a@example.com")
    )
    agent_id = agent.id
    response = await client.post(
        f"/api/tickets/{ticket['ticket_id']}/diagnose/apply",
        headers=headers,
        json={
            "diagnosis_id": body["diagnosis_id"],
            "next_step": body["recommendation"]["next_step"],
            "next_owner_id": str(agent_id),
            "next_follow_up_at": body["recommendation"]["next_follow_up_at"],
        },
    )
    assert response.status_code == 200, response.text
    applied = response.json()
    assert applied["next_owner"]["id"] == str(agent_id)

    detail = (await client.get(f"/api/tickets/{ticket['ticket_id']}")).json()
    assert detail["ticket"]["next_step"] == body["recommendation"]["next_step"]
    assert detail["next_owner"]["id"] == str(agent_id)
    assert detail["latest_diagnosis"]["path"] == "reuse"
    assert {owner["id"] for owner in detail["owners"]} >= {str(agent_id)}
    assert any(event["kind"] == "diagnosis_applied" for event in detail["events"])

    db.expire_all()
    followups = (
        await db.scalars(
            select(Followup).where(
                Followup.ticket_id == uuid.UUID(ticket["ticket_id"])
            )
        )
    ).all()
    assert any(
        followup.kind == "refund" and followup.owner_id == agent_id
        for followup in followups
    )


@pytest.mark.integration
async def test_diagnosis_requeries_stale_business_facts(
    client, seeded, inbound_sender, api_login, db
):
    ticket = await create_ticket(client, inbound_sender, "JP-STAGE4-REQUERY")
    headers = await login_headers(api_login, client)
    await process(client, headers, ticket["ticket_id"])

    db.expire_all()
    master = await db.scalar(
        select(AgentRun).where(
            AgentRun.ticket_id == uuid.UUID(ticket["ticket_id"]),
            AgentRun.graph == "master",
        )
    )
    await db.execute(
        update(ToolCall)
        .where(
            ToolCall.agent_run_id == master.id,
            ToolCall.tool_name == "shipment_track",
        )
        .values(
            result_status=ToolCallStatus.timeout,
            result_summary={"error": "timeout"},
        )
    )
    await db.commit()

    body = await _diagnose(client, headers, ticket["ticket_id"])
    assert body["path"] == "requery"
    assert body["requires_manual_review"] is False
    requery = [
        item for item in body["evidence"]["tool_calls"] if item["source"] == "requery"
    ]
    assert [item["tool_name"] for item in requery] == [
        "order_get",
        "shipments_list",
        "shipment_track",
        "inventory_get",
    ]
    assert all(item["status"] == "ok" for item in requery)
    assert any("重查" in reason or "重新" in reason for reason in body["reasons"])
    assert "追跡" not in body["recommendation"]["next_step"]

    db.expire_all()
    run = await db.get(AgentRun, uuid.UUID(body["diagnosis_id"]))
    stored = (
        await db.scalars(
            select(ToolCall)
            .where(ToolCall.agent_run_id == run.id)
            .order_by(ToolCall.step_no)
        )
    ).all()
    assert len(stored) == 4
    assert all(row.result_status is ToolCallStatus.ok for row in stored)


@pytest.mark.integration
async def test_diagnosis_manual_path_and_supervisor_owner_writeback(
    client, seeded, inbound_sender, api_login, db
):
    ticket = await create_ticket(client, inbound_sender, "JP-STAGE4-MANUAL")
    headers = await login_headers(api_login, client)
    await process(client, headers, ticket["ticket_id"])

    db.expire_all()
    master = await db.scalar(
        select(AgentRun).where(
            AgentRun.ticket_id == uuid.UUID(ticket["ticket_id"]),
            AgentRun.graph == "master",
        )
    )
    await db.execute(
        update(ToolCall)
        .where(
            ToolCall.agent_run_id == master.id,
            ToolCall.tool_name == "order_get",
        )
        .values(
            result_status=ToolCallStatus.denied,
            denied_reason="resource_out_of_scope",
            result_summary={"error": "not_found", "policy_reason": "resource_out_of_scope"},
        )
    )
    await db.commit()

    body = await _diagnose(client, headers, ticket["ticket_id"])
    assert body["path"] == "manual"
    assert body["requires_manual_review"] is True
    assert any("拒绝" in reason for reason in body["reasons"])
    assert not [
        item for item in body["evidence"]["tool_calls"] if item["source"] == "requery"
    ]

    db.expire_all()
    run = await db.get(AgentRun, uuid.UUID(body["diagnosis_id"]))
    assert not (
        await db.scalars(select(ToolCall).where(ToolCall.agent_run_id == run.id))
    ).all()

    agent = await db.scalar(
        select(Employee).where(Employee.email == "cs.a@example.com")
    )
    supervisor = await db.scalar(
        select(Employee).where(Employee.email == "sup.a@example.com")
    )
    agent_id = agent.id
    supervisor_id = supervisor.id
    forbidden = await client.post(
        f"/api/tickets/{ticket['ticket_id']}/diagnose/apply",
        headers=headers,
        json={
            "diagnosis_id": body["diagnosis_id"],
            "next_step": body["recommendation"]["next_step"],
            "next_owner_id": str(supervisor_id),
        },
    )
    assert forbidden.status_code == 403

    supervisor_headers = await login_headers(
        api_login, client, "sup.a@example.com"
    )
    allowed = await client.post(
        f"/api/tickets/{ticket['ticket_id']}/diagnose/apply",
        headers=supervisor_headers,
        json={
            "diagnosis_id": body["diagnosis_id"],
            "next_step": body["recommendation"]["next_step"],
            "next_owner_id": str(agent_id),
            "next_follow_up_at": body["recommendation"]["next_follow_up_at"],
        },
    )
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["path"] == "manual"
    assert allowed.json()["next_owner"]["id"] == str(agent_id)

    db.expire_all()
    stored_ticket = await db.get(Ticket, uuid.UUID(ticket["ticket_id"]))
    assert stored_ticket.next_owner_id == agent_id
    assert stored_ticket.assignee_id == agent_id
    assert stored_ticket.next_follow_up_at is not None