from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.mcp.client import open_readonly_session, open_training_session
from app.mcp.context import TRAINING_TOOL_NAMES, RunContext, run_context_token
from app.models import AgentRun, Employee, Store, ToolCall, ToolCallStatus

from stage2_helpers import login_headers

GOOD_ANSWER = (
    "先核对工单、买家与订单归属；联系承运商发起调查并记录查询编号；"
    "调查完成前不承诺退款；退款由具备退款权限的客服跟进；"
    "所有日语回复经人工审核后发送；记录结论、依据和告知状态后结案。"
)


@pytest.mark.integration
async def test_training_agent_uses_only_case_and_fixed_sop(
    client, seeded, api_login, db, app_instance
):
    headers = await login_headers(api_login, client)
    response = await client.get("/api/training/cases", headers=headers)
    assert response.status_code == 200, response.text
    cases = response.json()["items"]
    assert len(cases) == 1
    case = cases[0]
    assert case["sop"]["version"] == 1
    assert "body" not in case["sop"]

    detail = await client.get(f"/api/training/cases/{case['id']}", headers=headers)
    assert detail.status_code == 200
    assert detail.json()["anonymized_content"]

    tenant_b_headers = await login_headers(api_login, client, "cs.b@example.com")
    assert (
        await client.get("/api/training/cases", headers=tenant_b_headers)
    ).json()["items"] == []
    assert (
        await client.get(f"/api/training/cases/{case['id']}", headers=tenant_b_headers)
    ).status_code == 404

    headers = await login_headers(api_login, client, "cs.a@example.com")
    poor = await client.post(
        f"/api/training/cases/{case['id']}/attempts",
        headers=headers,
        json={"answer": "我不确定如何处理。"},
    )
    assert poor.status_code == 200, poor.text
    poor_body = poor.json()
    assert poor_body["revision_no"] == 1
    assert poor_body["feedback"]["verdict"] == "needs_revision"
    assert poor_body["feedback"]["required_revision"] is True
    assert "carrier_investigation" in poor_body["feedback"]["missing"]
    assert poor_body["score"] < 80
    assert poor_body["trajectory"]["graph"] == "training"
    assert [item["tool_name"] for item in poor_body["trajectory"]["tools"]] == [
        "training_case_get",
        "training_sop_get",
    ]
    assert poor_body["trajectory"]["sop"]["checksum"] == case["sop"]["checksum"]

    revised = await client.post(
        f"/api/training/cases/{case['id']}/attempts/{poor_body['attempt_id']}/revise",
        headers=headers,
        json={"answer": GOOD_ANSWER},
    )
    assert revised.status_code == 200, revised.text
    revised_body = revised.json()
    assert revised_body["revision_no"] == 2
    assert revised_body["feedback"]["verdict"] == "passed"
    assert revised_body["feedback"]["missing"] == []
    assert revised_body["feedback"]["critical_missing"] == []
    assert revised_body["score"] >= 80
    assert len(revised_body["feedback"]["matched"]) == 6

    history = await client.get(
        f"/api/training/cases/{case['id']}/attempts", headers=headers
    )
    assert [item["revision_no"] for item in history.json()["items"]] == [1, 2]

    db.expire_all()
    runs = (
        await db.scalars(select(AgentRun).where(AgentRun.graph == "training"))
    ).all()
    assert len(runs) == 2
    run_ids = [run.id for run in runs]
    calls = (
        await db.scalars(select(ToolCall).where(ToolCall.agent_run_id.in_(run_ids)))
    ).all()
    assert {call.tool_name for call in calls} == {
        "training_case_get",
        "training_sop_get",
    }
    assert all(call.result_status is ToolCallStatus.ok for call in calls)

    employee = await db.scalar(
        select(Employee).where(Employee.email == "cs.a@example.com")
    )
    store = await db.scalar(select(Store).where(Store.code == "A-JP-01"))
    settings = app_instance.state.settings
    context = RunContext(
        tenant_id=employee.tenant_id,
        store_ids={store.id},
        employee_id=str(employee.id),
        role="agent",
        allowed_tools=TRAINING_TOOL_NAMES,
        ticket_id=None,
        agent_run_id=None,
        graph="training",
    )
    token = run_context_token(settings, context)

    async with open_training_session(
        settings, app_instance.state.mcp_sessionmaker
    ) as session:
        listed = await session.list_tools()
        assert {tool.name for tool in listed.tools} == TRAINING_TOOL_NAMES
        hidden = await session.call_tool(
            "order_get",
            {"order_no": "A-JP-1001"},
            meta={"run_context": token, "tool_call_id": "tc-hidden"},
            read_timeout_seconds=None,
        )
        assert hidden.isError is True
        assert hidden.structuredContent["policy_reason"] == "tool_not_allowlisted"

    async with open_readonly_session(
        settings,
        app_instance.state.commerce_adapter,
        app_instance.state.scope_resolver,
    ) as business_session:
        blocked = await business_session.call_tool(
            "order_get",
            {"order_no": "A-JP-1001"},
            meta={"run_context": token, "tool_call_id": "tc-training-token"},
            read_timeout_seconds=None,
        )
        assert blocked.isError is True
        assert blocked.structuredContent["policy_reason"] == "tool_not_allowlisted"