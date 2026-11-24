from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx

from app.config import get_settings
from app.security import sign_hmac

ROOT = Path(__file__).resolve().parents[1]
SAMPLE_TEXT = "配送状況を確認したいです。"
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"stage5-demo-attachment"


def login(client: httpx.Client, email: str, password: str) -> dict:
    response = client.post(
        "/login",
        data={"email": email, "password": password},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    me = client.get("/api/auth/me")
    me.raise_for_status()
    return me.json()


def inbound(client: httpx.Client, settings, external_id: str, buyer_id: str) -> dict:
    payload = {
        "external_message_id": external_id,
        "buyer_external_id": buyer_id,
        "buyer_display_name": "山田 花子",
        "text": SAMPLE_TEXT,
        "order_candidates": ["A-JP-1001"],
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    response = client.post(
        "/api/inbound/a-jp-01-amazon",
        content=body,
        headers={
            "content-type": "application/json",
            "X-Channel-Signature": sign_hmac(
                f"{settings.test_channel_secret}-a-jp-01-amazon", body
            ),
        },
    )
    response.raise_for_status()
    return response.json()


def main() -> None:
    settings = get_settings()
    parser = argparse.ArgumentParser(description="Verify stage 5 attachments, OCR and integration status")
    parser.add_argument("--base-url", default=settings.app_base_url)
    parser.add_argument("--email", default="cs.a@example.com")
    parser.add_argument("--output", default=str(ROOT / "artifacts" / "stage5-api.json"))
    args = parser.parse_args()
    stamp = datetime.now().strftime("%Y%m%d%H%M%S")

    with httpx.Client(base_url=args.base_url, timeout=60) as client:
        me = login(client, args.email, settings.seed_password)
        headers = {"X-CSRF-Token": me["csrf_token"]}

        status = client.get("/api/integrations/status")
        status.raise_for_status()
        status_body = status.json()
        assert status_body["per_store_enablement"] is True
        assert status_body["enabled_store_ids"] == []
        assert all(
            entry["active"] is False
            for entry in status_body["integrations"].values()
        ), status_body

        created = inbound(
            client,
            settings,
            f"JP-STAGE5-DEMO-{stamp}",
            f"buyer-stage5-{stamp}",
        )
        ticket_id = created["ticket_id"]

        upload = client.post(
            f"/api/tickets/{ticket_id}/attachments",
            headers=headers,
            files={"file": ("stage5-receipt.png", PNG_BYTES, "image/png")},
        )
        upload.raise_for_status()
        attachment = upload.json()
        attachment_url = f"/api/tickets/{ticket_id}/attachments/{attachment['id']}"

        download = client.get(attachment_url)
        download.raise_for_status()
        assert download.content == PNG_BYTES
        assert download.headers["content-type"].startswith("image/png")

        ocr = client.post(f"{attachment_url}/ocr", headers=headers)
        ocr.raise_for_status()
        ocr_body = ocr.json()
        assert ocr_body["provider"] == "fake"
        assert "本地 OCR 模拟" in ocr_body["text"]

        client.cookies.clear()
        kb = login(client, "kb.a@example.com", settings.seed_password)
        index_status = client.get("/api/knowledge/index")
        index_status.raise_for_status()
        index_body = index_status.json()
        assert "snapshots" in index_body

        artifact = {
            "generated_at": stamp,
            "ticket_id": ticket_id,
            "attachment": {
                "id": attachment["id"],
                "filename": attachment["filename"],
                "size": attachment["size"],
                "sha256": attachment["sha256"],
                "object_key": attachment["object_key"],
            },
            "download_matches": True,
            "ocr": ocr_body,
            "integration_status": status_body,
            "index_status": {
                "active": index_body["active"],
                "snapshot_count": len(index_body["snapshots"]),
            },
            "knowledge_admin": kb["employee"]["email"],
        }
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(artifact, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(json.dumps(artifact, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()