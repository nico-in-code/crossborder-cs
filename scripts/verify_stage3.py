from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx

from app.config import get_settings
from app.security import sign_hmac

ROOT = Path(__file__).resolve().parents[1]
SAMPLE_TEXT = (
    "配送状況では配達済みになっていますが、荷物が届いていません。"
    "返金を申請したいです。"
)
CONCLUSION = "承运商确认投递地址异常，已提交退款申请并通过站内信告知买家。"
BASIS = "承运商轨迹显示投递地址与订单地址不一致；退款单 R-2026-001 已受理。"


def inbound(client: httpx.Client, settings, account: str, external_id: str) -> dict:
    payload = {
        "external_message_id": external_id,
        "buyer_external_id": "buyer-jp-001",
        "buyer_display_name": "山田 花子",
        "text": SAMPLE_TEXT,
        "order_candidates": ["A-JP-1001"],
        "tenant_id": "forged-tenant",
        "store_id": "forged-store",
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    response = client.post(
        f"/api/inbound/{account}",
        content=body,
        headers={
            "content-type": "application/json",
            "X-Channel-Signature": sign_hmac(
                f"{settings.test_channel_secret}-{account}", body
            ),
        },
    )
    response.raise_for_status()
    return response.json()


def detail(client: httpx.Client, ticket_id: str) -> dict:
    response = client.get(f"/api/tickets/{ticket_id}")
    response.raise_for_status()
    return response.json()


def wait_for_send(client: httpx.Client, ticket_id: str, timeout: float = 25.0) -> dict:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = detail(client, ticket_id)
        tasks = last["send_tasks"]
        if (
            tasks
            and tasks[0]["status"] in {"acked", "reconciled_sent"}
            and last["ticket"]["status"] != "awaiting_review"
        ):
            return last
        time.sleep(0.5)
    raise AssertionError(f"send did not complete: {last['send_tasks'] if last else None}")


def prepare(client, settings, headers, ticket_id: str) -> dict:
    response = client.post(f"/api/tickets/{ticket_id}/process", headers=headers)
    response.raise_for_status()
    return detail(client, ticket_id)


def approve(client, headers, ticket_id: str, draft: dict) -> dict:
    response = client.post(
        f"/api/tickets/{ticket_id}/drafts/{draft['id']}/approve",
        headers=headers,
        json={"content_hash": draft["content_hash"]},
    )
    response.raise_for_status()
    return response.json()


def main() -> None:
    settings = get_settings()
    parser = argparse.ArgumentParser(description="Verify stage 3 review/send/close flow")
    parser.add_argument("--base-url", default=settings.app_base_url)
    parser.add_argument("--email", default="cs.a@example.com")
    parser.add_argument("--output", default=str(ROOT / "artifacts" / "stage3-api.json"))
    args = parser.parse_args()
    stamp = datetime.now().strftime("%Y%m%d%H%M%S")

    with httpx.Client(base_url=args.base_url, timeout=30) as client:
        login = client.post(
            "/login",
            data={"email": args.email, "password": settings.seed_password},
            follow_redirects=False,
        )
        assert login.status_code == 303, login.text
        me = client.get("/api/auth/me")
        me.raise_for_status()
        headers = {"X-CSRF-Token": me.json()["csrf_token"]}

        golden = inbound(
            client, settings, "a-jp-01-amazon", f"JP-STAGE3-DEMO-{stamp}"
        )
        assert golden["duplicate"] is False, golden
        golden_id = golden["ticket_id"]
        prepared = prepare(client, settings, headers, golden_id)
        assert prepared["workflow"]["next"] == ["await_review"]
        assert prepared["latest_draft"] is not None
        approved = approve(client, headers, golden_id, prepared["latest_draft"])

        sent = wait_for_send(client, golden_id)
        assert sent["ticket"]["status"] == "open", sent["ticket"]
        assert sent["send_tasks"][0]["id"] == approved["send_task_id"]
        assert sent["send_tasks"][0]["status"] == "acked"
        assert sent["send_attempts"][0]["outcome"] == "channel_accepted"
        followup = [item for item in sent["followups"] if item["kind"] == "refund"][0]
        assert followup["status"] == "pending"

        updated = client.post(
            f"/api/tickets/{golden_id}/followups/{followup['id']}",
            headers=headers,
            json={"conclusion": CONCLUSION, "basis": BASIS, "notified": True},
        )
        updated.raise_for_status()
        closed = client.post(
            f"/api/tickets/{golden_id}/close",
            headers=headers,
            json={"conclusion": CONCLUSION, "basis": BASIS, "notified": True},
        )
        closed.raise_for_status()
        final = detail(client, golden_id)
        assert final["ticket"]["status"] == "closed"
        assert final["followups"][0]["notified_at"] is not None

        edited_ticket = inbound(
            client, settings, "a-jp-01-amazon", f"JP-STAGE3-EDITDEMO-{stamp}"
        )
        assert edited_ticket["duplicate"] is False, edited_ticket
        assert edited_ticket["ticket_id"] != golden_id
        edited_id = edited_ticket["ticket_id"]
        prepared = prepare(client, settings, headers, edited_id)
        first = approve(client, headers, edited_id, prepared["latest_draft"])
        first_sent = wait_for_send(client, edited_id)
        first_hash = prepared["latest_draft"]["content_hash"]

        edit = client.post(
            f"/api/tickets/{edited_id}/drafts",
            headers=headers,
            json={
                "body": "編集済みの回答文です。返金手続きの流れをご案内します。",
                "note": "stage3 demo edit",
            },
        )
        edit.raise_for_status()
        edited = edit.json()
        blocked = detail(client, edited_id)
        old_task = next(
            task
            for task in blocked["send_tasks"]
            if task["id"] == first["send_task_id"]
        )
        assert old_task["payload"]["content_hash"] == first_hash
        assert old_task["id"] == first_sent["send_tasks"][0]["id"]
        assert all(not review["is_current"] for review in blocked["reviews"])
        assert blocked["latest_draft"]["id"] == edited["draft_id"]

        second = approve(client, headers, edited_id, blocked["latest_draft"])
        assert second["draft_version"] == edited["version"] == 2
        resent = wait_for_send(client, edited_id)
        new_task = next(
            task
            for task in resent["send_tasks"]
            if task["id"] == second["send_task_id"]
        )
        assert new_task["payload"]["content_hash"] == edited["content_hash"]
        assert new_task["status"] == "acked"

        edited_followup = [
            item for item in resent["followups"] if item["kind"] == "refund"
        ][0]
        updated = client.post(
            f"/api/tickets/{edited_id}/followups/{edited_followup['id']}",
            headers=headers,
            json={
                "conclusion": "改稿版本已重新审核发送，退款跟进完成并告知买家。",
                "basis": "v2 审核记录与发送回执。",
                "notified": True,
            },
        )
        updated.raise_for_status()
        closed_edit = client.post(
            f"/api/tickets/{edited_id}/close",
            headers=headers,
            json={
                "conclusion": "改稿版本已重新审核发送，退款跟进完成并告知买家。",
                "basis": "v2 审核记录与发送回执。",
                "notified": True,
            },
        )
        closed_edit.raise_for_status()

        html = client.get(f"/tickets/{golden_id}")
        html.raise_for_status()
        assert SAMPLE_TEXT in html.text
        assert "审核与发送" in html.text
        assert "结案" in html.text

        artifact = Path(args.output)
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text(
            json.dumps(
                {
                    "golden_ticket": golden_id,
                    "golden_status": final["ticket"]["status"],
                    "golden_send_status": sent["send_tasks"][0]["status"],
                    "golden_external_send_id": sent["send_attempts"][0]["external_send_id"],
                    "edited_ticket": edited_id,
                    "old_task_hash_preserved": old_task["payload"]["content_hash"] == first_hash,
                    "edited_task_status": new_task["status"],
                    "edited_version": edited["version"],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        (ROOT / "artifacts" / "stage3-ticket.html").write_text(
            html.text, encoding="utf-8"
        )

    print(
        json.dumps(
            {
                "golden_ticket": golden_id,
                "send_status": sent["send_tasks"][0]["status"],
                "ticket_status": final["ticket"]["status"],
                "old_task_hash_preserved": old_task["payload"]["content_hash"] == first_hash,
                "edited_version_sent": new_task["status"],
                "artifact": str(Path(args.output)),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()