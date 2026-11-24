from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models import Employee

SAMPLE_TEXT = (
    "配送状況では配達済みになっていますが、荷物が届いていません。"
    "返金を申請したいです。"
)


async def _create_ticket(client, inbound_sender, account, external_id, order):
    response = await inbound_sender(
        client,
        account,
        {
            "external_message_id": external_id,
            "buyer_external_id": "buyer-admin-001",
            "text": SAMPLE_TEXT,
            "order_candidates": [order],
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["ticket_id"]


def _headers(login: dict) -> dict:
    return {"X-CSRF-Token": login["csrf_token"]}


async def _employees(client) -> dict:
    response = await client.get("/api/admin/employees")
    assert response.status_code == 200, response.text
    return response.json()


def _by_email(payload: dict, email: str) -> dict:
    return next(item for item in payload["employees"] if item["email"] == email)


@pytest.mark.integration
async def test_role_change_takes_effect_and_self_lockout_guarded(
    client, seeded, inbound_sender, api_login, settings
):
    ticket_id = await _create_ticket(
        client, inbound_sender, "a-jp-01-amazon", "JP-ADMIN-0001", "A-JP-1001"
    )

    agent_login = await api_login(client, "cs.a@example.com")
    assert (await client.get("/api/admin/employees")).status_code == 403
    denied = await client.post(
        f"/api/tickets/{ticket_id}/assign",
        json={"assignee_id": "00000000-0000-0000-0000-000000000000"},
        headers=_headers(agent_login),
    )
    assert denied.status_code == 403

    admin_login = await api_login(client, "admin.a@example.com")
    payload = await _employees(client)
    admin = _by_email(payload, "admin.a@example.com")
    agent = _by_email(payload, "cs.a@example.com")
    supervisor = _by_email(payload, "sup.a@example.com")
    assert agent["role"] == "agent"
    assert [store["code"] for store in agent["stores"]] == ["A-JP-01"]

    self_role = await client.patch(
        f"/api/admin/employees/{admin['id']}",
        json={"role": "agent"},
        headers=_headers(admin_login),
    )
    assert self_role.status_code == 409
    self_deactivate = await client.patch(
        f"/api/admin/employees/{admin['id']}",
        json={"active": False},
        headers=_headers(admin_login),
    )
    assert self_deactivate.status_code == 409

    promoted = await client.patch(
        f"/api/admin/employees/{agent['id']}",
        json={"role": "supervisor"},
        headers=_headers(admin_login),
    )
    assert promoted.status_code == 200, promoted.text
    assert promoted.json()["role"] == "supervisor"

    promoted_login = await api_login(client, "cs.a@example.com")
    assert promoted_login["employee"]["role"] == "supervisor"
    assigned = await client.post(
        f"/api/tickets/{ticket_id}/assign",
        json={"assignee_id": supervisor["id"]},
        headers=_headers(promoted_login),
    )
    assert assigned.status_code == 200, assigned.text

    admin_login = await api_login(client, "admin.a@example.com")
    deactivated = await client.patch(
        f"/api/admin/employees/{agent['id']}",
        json={"active": False},
        headers=_headers(admin_login),
    )
    assert deactivated.status_code == 200
    assert deactivated.json()["active"] is False
    blocked = await client.post(
        "/api/auth/login",
        json={"email": "cs.a@example.com", "password": settings.seed_password},
    )
    assert blocked.status_code == 401

    await api_login(client, "admin.a@example.com")
    audits = (await _employees(client))["audits"]
    role_audit = next(
        audit
        for audit in audits
        if audit["action"] == "employee_updated"
        and "角色 客服 → 主管" in audit["summary"]
    )
    assert role_audit["target_name"] == "客服 小林"
    assert "状态 启用 → 停用" in next(
        audit["summary"]
        for audit in audits
        if audit["action"] == "employee_updated" and audit["target_name"] == "客服 小林"
    )


@pytest.mark.integration
async def test_store_grants_control_ticket_visibility_and_tenant_scope(
    client, seeded, inbound_sender, api_login, db
):
    a2_ticket = await _create_ticket(
        client, inbound_sender, "a-jp-02-amazon", "JP-ADMIN-0002", "A2-JP-2001"
    )

    await api_login(client, "cs.a@example.com")
    assert (await client.get(f"/api/tickets/{a2_ticket}")).status_code == 404

    admin_login = await api_login(client, "admin.a@example.com")
    payload = await _employees(client)
    agent = _by_email(payload, "cs.a@example.com")
    stores = {store["code"]: store for store in payload["stores"]}
    assert "A-JP-02" in stores

    granted = await client.post(
        f"/api/admin/employees/{agent['id']}/grants",
        json={"store_id": stores["A-JP-02"]["id"]},
        headers=_headers(admin_login),
    )
    assert granted.status_code == 200, granted.text
    repeated = await client.post(
        f"/api/admin/employees/{agent['id']}/grants",
        json={"store_id": stores["A-JP-02"]["id"]},
        headers=_headers(admin_login),
    )
    assert repeated.status_code == 200
    assert repeated.json()["idempotent"] is True

    await api_login(client, "cs.a@example.com")
    assert (await client.get(f"/api/tickets/{a2_ticket}")).status_code == 200
    listing = (await client.get("/api/tickets")).json()["items"]
    assert {item["store"]["code"] for item in listing} == {"A-JP-02"}

    admin_login = await api_login(client, "admin.a@example.com")
    revoked = await client.delete(
        f"/api/admin/employees/{agent['id']}/grants/{stores['A-JP-02']['id']}",
        headers=_headers(admin_login),
    )
    assert revoked.status_code == 200, revoked.text

    await api_login(client, "cs.a@example.com")
    assert (await client.get(f"/api/tickets/{a2_ticket}")).status_code == 404

    admin_login = await api_login(client, "admin.a@example.com")
    payload = await _employees(client)
    assert all(item["email"] != "cs.b@example.com" for item in payload["employees"])
    kb = _by_email(payload, "kb.a@example.com")
    non_ticket_grant = await client.post(
        f"/api/admin/employees/{kb['id']}/grants",
        json={"store_id": stores["A-JP-01"]["id"]},
        headers=_headers(admin_login),
    )
    assert non_ticket_grant.status_code == 409

    cross_tenant_id = await db.scalar(
        select(Employee.id).where(Employee.email == "cs.b@example.com")
    )
    assert cross_tenant_id is not None
    cross_tenant = await client.patch(
        f"/api/admin/employees/{cross_tenant_id}",
        json={"role": "supervisor"},
        headers=_headers(admin_login),
    )
    assert cross_tenant.status_code == 404

    promoted_kb = await client.patch(
        f"/api/admin/employees/{kb['id']}",
        json={"role": "agent"},
        headers=_headers(admin_login),
    )
    assert promoted_kb.status_code == 200
    audits = (await _employees(client))["audits"]
    assert any(
        audit["action"] == "employee_updated" and audit["target_name"] == "知识管理员 佐藤"
        for audit in audits
    )
    assert any(audit["action"] == "store_grant_added" for audit in audits)
    assert any(audit["action"] == "store_grant_removed" for audit in audits)