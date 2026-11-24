from __future__ import annotations

import re

import pytest

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
            "buyer_external_id": "buyer-jp-001",
            "buyer_display_name": "山田 花子",
            "text": SAMPLE_TEXT,
            "order_candidates": [order],
        },
    )
    assert response.status_code == 200
    return response.json()


@pytest.mark.e2e
async def test_workbench_detail_renders_full_japanese_text(
    client, app_instance, seeded, inbound_sender, settings, tmp_path
):
    ticket = await _create_ticket(
        client, inbound_sender, "a-jp-01-amazon", "JP-HTML-0001", "A-JP-1001"
    )

    login = await client.post(
        "/login",
        data={"email": "cs.a@example.com", "password": settings.seed_password},
    )
    assert login.status_code == 303
    assert login.headers["location"] == "/tickets"

    listing = await client.get("/tickets")
    assert listing.status_code == 200
    assert ticket["ticket_number"] in listing.text

    app_instance.state.settings.attachment_root = str(tmp_path)
    csrf = re.search(
        r'name="csrf-token" content="([^"]+)"', listing.text
    ).group(1)
    upload = await client.post(
        f"/api/tickets/{ticket['ticket_id']}/attachments",
        headers={"X-CSRF-Token": csrf},
        files={"file": ("receipt.png", b"\x89PNG\r\n\x1a\nhtml-e2e", "image/png")},
    )
    assert upload.status_code == 200, upload.text

    detail = await client.get(f"/tickets/{ticket['ticket_id']}")
    assert detail.status_code == 200
    assert "text/html" in detail.headers["content-type"]
    assert SAMPLE_TEXT in detail.text
    assert "中文译文" in detail.text
    assert "待翻译" in detail.text
    assert ticket["ticket_number"] in detail.text
    assert "附件与 OCR" in detail.text
    assert "receipt.png" in detail.text
    assert "ocr-attachment" in detail.text
    assert 'id="upload-attachment-form"' in detail.text


@pytest.mark.e2e
async def test_login_page_and_redirects(client):
    page = await client.get("/login")
    assert page.status_code == 200
    assert "客服工单工作台" in page.text
    assert 'name="email"' in page.text

    listing = await client.get("/tickets", follow_redirects=False)
    assert listing.status_code == 303
    assert listing.headers["location"] == "/login"


@pytest.mark.e2e
async def test_unauthorized_detail_renders_404_page(
    client, seeded, inbound_sender, settings
):
    ticket = await _create_ticket(
        client, inbound_sender, "a-jp-01-amazon", "JP-HTML-0002", "A-JP-1001"
    )

    login = await client.post(
        "/login",
        data={"email": "kb.a@example.com", "password": settings.seed_password},
    )
    assert login.status_code == 303

    detail = await client.get(f"/tickets/{ticket['ticket_id']}")
    assert detail.status_code == 403
    assert "没有执行该操作的权限" in detail.text
    assert SAMPLE_TEXT not in detail.text


async def _login(client, settings, email: str) -> None:
    response = await client.post(
        "/login", data={"email": email, "password": settings.seed_password}
    )
    assert response.status_code == 303


@pytest.mark.e2e
async def test_knowledge_page_requires_knowledge_admin(client, seeded, settings):
    await _login(client, settings, "cs.a@example.com")
    forbidden = await client.get("/knowledge")
    assert forbidden.status_code == 403
    assert "没有知识索引管理权限" in forbidden.text

    client.cookies.clear()
    await _login(client, settings, "kb.a@example.com")
    page = await client.get("/knowledge")
    assert page.status_code == 200
    assert "知识索引" in page.text
    assert "发布新快照" in page.text
    assert 'id="publish-index"' in page.text


@pytest.mark.e2e
async def test_integrations_page_requires_system_admin(client, seeded, settings):
    await _login(client, settings, "cs.a@example.com")
    forbidden = await client.get("/integrations")
    assert forbidden.status_code == 403
    assert "没有集成状态查看权限" in forbidden.text

    client.cookies.clear()
    await _login(client, settings, "admin.a@example.com")
    page = await client.get("/integrations")
    assert page.status_code == 200
    assert "集成状态" in page.text
    assert "Amazon SP-API 订单" in page.text
    assert "inactive" in page.text


@pytest.mark.e2e
async def test_employees_page_requires_system_admin(client, seeded, settings):
    await _login(client, settings, "cs.a@example.com")
    forbidden = await client.get("/employees")
    assert forbidden.status_code == 403
    assert "没有员工权限管理权限" in forbidden.text

    client.cookies.clear()
    await _login(client, settings, "admin.a@example.com")
    page = await client.get("/employees")
    assert page.status_code == 200
    assert "员工与权限" in page.text
    assert 'id="employee-table"' in page.text
    assert "权限变更审计" in page.text
    assert "cs.a@example.com" in page.text
    assert "cs.b@example.com" not in page.text
    assert 'href="/employees"' in page.text