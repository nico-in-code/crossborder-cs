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
SAMPLE_TEXT = (
    "配送状況では配達済みになっていますが、荷物が届いていません。"
    "返金を申請したいです。"
)
GOOD_ANSWER = (
    "先核对工单、买家与订单归属；联系承运商发起调查并记录查询编号；"
    "调查完成前不承诺退款；退款由具备退款权限的客服跟进；"
    "所有日语回复经人工审核后发送；记录结论、依据和告知状态后结案。"
)


def inbound(
    client: httpx.Client, settings, account: str, external_id: str, buyer_id: str
) -> dict:
    payload = {
        "external_message_id": external_id,
        "buyer_external_id": buyer_id,
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


def main() -> None:
    settings = get_settings()
    parser = argparse.ArgumentParser(description="Verify stage 4 diagnosis and training flow")
    parser.add_argument("--base-url", default=settings.app_base_url)
    parser.add_argument("--email", default="cs.a@example.com")
    parser.add_argument("--output", default=str(ROOT / "artifacts" / "stage4-api.json"))
    args = parser.parse_args()
    stamp = datetime.now().strftime("%Y%m%d%H%M%S")

    with httpx.Client(base_url=args.base_url, timeout=60) as client:
        login = client.post(
            "/login",
            data={"email": args.email, "password": settings.seed_password},
            follow_redirects=False,
        )
        assert login.status_code == 303, login.text
        me = client.get("/api/auth/me")
        me.raise_for_status()
        employee_id = me.json()["employee"]["id"]
        headers = {"X-CSRF-Token": me.json()["csrf_token"]}

        created = inbound(
            client,
            settings,
            "a-jp-01-amazon",
            f"JP-STAGE4-DEMO-{stamp}",
            f"buyer-jp-{stamp}",
        )
        assert created["duplicate"] is False, created
        ticket_id = created["ticket_id"]
        processed = client.post(f"/api/tickets/{ticket_id}/process", headers=headers)
        processed.raise_for_status()

        diagnosis = client.post(
            f"/api/tickets/{ticket_id}/diagnose", headers=headers
        )
        diagnosis.raise_for_status()
        body = diagnosis.json()
        assert body["path"] in {"reuse", "requery", "manual"}, body
        assert body["evidence"]["sop"]["version"] >= 1, body["evidence"]["sop"]
        assert len(body["evidence"]["messages"]) == 1, body["evidence"]["messages"]
        assert body["evidence"]["draft"]["version"] == 1, body["evidence"]["draft"]
        assert body["recommendation"]["next_step"], body["recommendation"]

        applied = client.post(
            f"/api/tickets/{ticket_id}/diagnose/apply",
            headers=headers,
            json={
                "diagnosis_id": body["diagnosis_id"],
                "next_step": body["recommendation"]["next_step"],
                "next_owner_id": employee_id,
                "next_follow_up_at": body["recommendation"]["next_follow_up_at"],
                "note": "stage4 demo confirm",
            },
        )
        applied.raise_for_status()
        applied_body = applied.json()
        assert applied_body["next_owner"]["id"] == employee_id, applied_body
        assert applied_body["followup_id"], applied_body

        after = detail(client, ticket_id)
        assert after["ticket"]["next_step"] == body["recommendation"]["next_step"]
        assert after["next_owner"]["id"] == employee_id
        assert after["latest_diagnosis"]["path"] == body["path"]
        assert any(event["kind"] == "diagnosis_applied" for event in after["events"])

        cases = client.get("/api/training/cases", headers=headers)
        cases.raise_for_status()
        items = cases.json()["items"]
        assert items, "no approved training case seeded"
        case = items[0]
        case_id = case["id"]
        history_before = client.get(
            f"/api/training/cases/{case_id}/attempts", headers=headers
        ).json()["items"]
        start_revision = max(
            (item["revision_no"] for item in history_before), default=0
        )

        poor = client.post(
            f"/api/training/cases/{case_id}/attempts",
            headers=headers,
            json={"answer": "我不确定如何处理。"},
        )
        poor.raise_for_status()
        poor_body = poor.json()
        assert poor_body["revision_no"] == start_revision + 1, poor_body
        assert poor_body["feedback"]["verdict"] == "needs_revision", poor_body
        assert poor_body["trajectory"]["graph"] == "training"
        assert [tool["tool_name"] for tool in poor_body["trajectory"]["tools"]] == [
            "training_case_get",
            "training_sop_get",
        ]

        revised = client.post(
            f"/api/training/cases/{case_id}/attempts/{poor_body['attempt_id']}/revise",
            headers=headers,
            json={"answer": GOOD_ANSWER},
        )
        revised.raise_for_status()
        revised_body = revised.json()
        assert revised_body["revision_no"] == start_revision + 2, revised_body
        assert revised_body["feedback"]["verdict"] == "passed", revised_body
        assert revised_body["feedback"]["missing"] == [], revised_body

        history = client.get(
            f"/api/training/cases/{case_id}/attempts", headers=headers
        )
        history.raise_for_status()
        revisions = [item["revision_no"] for item in history.json()["items"]]
        assert revisions[-2:] == [start_revision + 1, start_revision + 2], revisions

        ticket_html = client.get(f"/tickets/{ticket_id}")
        ticket_html.raise_for_status()
        assert "工单诊断" in ticket_html.text
        assert body["recommendation"]["next_step"] in ticket_html.text

        training_html = client.get(f"/training/{case_id}")
        training_html.raise_for_status()
        assert "培训" in training_html.text
        assert case["sop"]["checksum"] in training_html.text

        artifact = Path(args.output)
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text(
            json.dumps(
                {
                    "ticket_id": ticket_id,
                    "diagnosis_path": body["path"],
                    "diagnosis_reasons": body["reasons"],
                    "sop_version": body["evidence"]["sop"]["version"],
                    "evidence_tools": [
                        item["tool_name"] for item in body["evidence"]["tool_calls"]
                    ],
                    "next_owner": applied_body["next_owner"]["display_name"],
                    "followup_id": applied_body["followup_id"],
                    "training_case": case["title"],
                    "first_verdict": poor_body["feedback"]["verdict"],
                    "revised_verdict": revised_body["feedback"]["verdict"],
                    "revisions": revisions[-2:],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        (ROOT / "artifacts" / "stage4-ticket.html").write_text(
            ticket_html.text, encoding="utf-8"
        )
        (ROOT / "artifacts" / "stage4-training.html").write_text(
            training_html.text, encoding="utf-8"
        )

    print(
        json.dumps(
            {
                "ticket_id": ticket_id,
                "path": body["path"],
                "sop_version": body["evidence"]["sop"]["version"],
                "next_owner": applied_body["next_owner"]["display_name"],
                "training_case": case["title"],
                "first_verdict": poor_body["feedback"]["verdict"],
                "revised_verdict": revised_body["feedback"]["verdict"],
                "artifact": str(artifact),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()