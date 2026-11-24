from __future__ import annotations

import hashlib
import uuid

import pytest

from app.adapters.ocr import HttpOcrAdapter
from app.config import Settings
from stage2_helpers import create_ticket, login_headers

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"stage5-attachment"


async def upload(client, headers, ticket_id, filename="receipt.png", data=PNG_BYTES):
    return await client.post(
        f"/api/tickets/{ticket_id}/attachments",
        headers=headers,
        files={"file": (filename, data, "image/png")},
    )


@pytest.mark.integration
async def test_upload_download_and_on_demand_ocr(
    client, app_instance, seeded, inbound_sender, api_login, tmp_path
):
    app_instance.state.settings.attachment_root = str(tmp_path)
    ticket = await create_ticket(
        client, inbound_sender, f"JP-ATT-{uuid.uuid4().hex[:8]}"
    )
    ticket_id = ticket["ticket_id"]
    headers = await login_headers(api_login, client)

    response = await upload(client, headers, ticket_id)
    assert response.status_code == 200, response.text
    attachment = response.json()
    assert attachment["filename"] == "receipt.png"
    assert attachment["content_type"] == "image/png"
    assert attachment["size"] == len(PNG_BYTES)
    assert attachment["sha256"] == hashlib.sha256(PNG_BYTES).hexdigest()
    assert attachment["access_scope"] == "ticket"
    assert ".." not in attachment["object_key"]
    attachment_url = f"/api/tickets/{ticket_id}/attachments/{attachment['id']}"

    download = await client.get(attachment_url)
    assert download.status_code == 200
    assert download.content == PNG_BYTES
    assert download.headers["content-type"].startswith("image/png")

    ocr = await client.post(f"{attachment_url}/ocr", headers=headers)
    assert ocr.status_code == 200, ocr.text
    ocr_body = ocr.json()
    assert ocr_body["provider"] == "fake"
    assert ocr_body["filename"] == "receipt.png"
    assert "本地 OCR 模拟" in ocr_body["text"]
    assert ocr_body["chars"] == len(ocr_body["text"])


@pytest.mark.integration
async def test_filename_traversal_is_sanitized(
    client, app_instance, seeded, inbound_sender, api_login, tmp_path
):
    app_instance.state.settings.attachment_root = str(tmp_path)
    ticket = await create_ticket(
        client, inbound_sender, f"JP-ATT-{uuid.uuid4().hex[:8]}"
    )
    ticket_id = ticket["ticket_id"]
    headers = await login_headers(api_login, client)

    response = await upload(
        client, headers, ticket_id, filename="../../etc/passwd.png"
    )
    assert response.status_code == 200, response.text
    attachment = response.json()
    assert attachment["filename"] == "passwd.png"
    assert attachment["object_key"].endswith(".png")
    assert ".." not in attachment["object_key"]


@pytest.mark.integration
async def test_attachment_is_scoped_to_ticket_and_tenant(
    client, app_instance, seeded, inbound_sender, api_login, tmp_path
):
    app_instance.state.settings.attachment_root = str(tmp_path)
    first = await create_ticket(
        client, inbound_sender, f"JP-ATT-{uuid.uuid4().hex[:8]}", buyer="buyer-att-a"
    )
    second = await create_ticket(
        client, inbound_sender, f"JP-ATT-{uuid.uuid4().hex[:8]}", buyer="buyer-att-b"
    )
    headers = await login_headers(api_login, client)
    response = await upload(client, headers, first["ticket_id"])
    assert response.status_code == 200, response.text
    attachment_id = response.json()["id"]

    wrong_ticket = await client.get(
        f"/api/tickets/{second['ticket_id']}/attachments/{attachment_id}"
    )
    assert wrong_ticket.status_code == 404

    await api_login(client, "cs.b@example.com")
    cross_tenant = await client.get(
        f"/api/tickets/{first['ticket_id']}/attachments/{attachment_id}"
    )
    assert cross_tenant.status_code == 404


@pytest.mark.integration
async def test_attachment_requires_login_and_size_limit(
    client, app_instance, seeded, inbound_sender, api_login, tmp_path
):
    app_instance.state.settings.attachment_root = str(tmp_path)
    ticket = await create_ticket(
        client, inbound_sender, f"JP-ATT-{uuid.uuid4().hex[:8]}"
    )
    ticket_id = ticket["ticket_id"]

    anonymous = await upload(client, {}, ticket_id)
    assert anonymous.status_code == 401

    headers = await login_headers(api_login, client)
    app_instance.state.settings.attachment_max_bytes = 4
    too_large = await upload(client, headers, ticket_id, data=b"12345")
    assert too_large.status_code == 413

    app_instance.state.ocr_adapter = HttpOcrAdapter(
        Settings(ocr_provider="http", ocr_base_url="")
    )
    app_instance.state.settings.attachment_max_bytes = 1024 * 1024
    created = await upload(client, headers, ticket_id)
    assert created.status_code == 200, created.text
    attachment_url = (
        f"/api/tickets/{ticket_id}/attachments/{created.json()['id']}"
    )
    ocr = await client.post(f"{attachment_url}/ocr", headers=headers)
    assert ocr.status_code == 503
    assert "ocr not configured" in ocr.json()["detail"]