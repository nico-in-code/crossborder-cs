from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models import AgentRun, ToolCall

from stage2_helpers import create_ticket, login_headers, process


@pytest.mark.integration
async def test_business_tools_query_order_before_shipment(
    client, seeded, inbound_sender, api_login, db
):
    ticket = await create_ticket(client, inbound_sender, "JP-STAGE2-ORDER")
    headers = await login_headers(api_login, client)
    result = await process(client, headers, ticket["ticket_id"])

    assert [call["tool_name"] for call in result["tool_calls"]] == [
        "order_get",
        "shipments_list",
        "shipment_track",
        "inventory_get",
    ]
    assert all(call["status"] == "ok" for call in result["tool_calls"])

    db.expire_all()
    run = await db.scalar(
        select(AgentRun).where(AgentRun.ticket_id == ticket["ticket_id"])
    )
    rows = (
        await db.scalars(
            select(ToolCall)
            .where(ToolCall.agent_run_id == run.id)
            .order_by(ToolCall.step_no)
        )
    ).all()
    steps = {row.tool_name: row.step_no for row in rows}
    assert steps["order_get"] < steps["shipments_list"] < steps["shipment_track"]

    order_row = next(row for row in rows if row.tool_name == "order_get")
    assert order_row.arguments == {"order_no": "A-JP-1001"}
    assert order_row.result_summary["order_no"] == "A-JP-1001"

    shipment_row = next(row for row in rows if row.tool_name == "shipments_list")
    assert "order_id" in shipment_row.arguments

    track_row = next(row for row in rows if row.tool_name == "shipment_track")
    assert track_row.arguments == {"tracking_no": "YMT-A-0001"}
    assert track_row.result_summary["delivery_status"] == "delivered"

    assert result["draft"]["structured_facts"]["carrier_records"]["order_no"] == "A-JP-1001"
    assert (
        result["draft"]["structured_facts"]["carrier_records"]["tracking_no"]
        == "YMT-A-0001"
    )