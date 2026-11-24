from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx

from app.config import get_settings

ROOT = Path(__file__).resolve().parents[1]
EXPECTED_TEXT = (
    "配送状況では配達済みになっていますが、荷物が届いていません。"
    "返金を申請したいです。"
)


def main() -> None:
    settings = get_settings()
    parser = argparse.ArgumentParser(description="Verify stage 2 ticket processing in the workbench")
    parser.add_argument("--base-url", default=settings.app_base_url)
    parser.add_argument("--email", default="cs.a@example.com")
    parser.add_argument("--output", default=str(ROOT / "artifacts" / "stage2-ticket.html"))
    parser.add_argument("--json-output", default=str(ROOT / "artifacts" / "stage2-api.json"))
    args = parser.parse_args()

    with httpx.Client(base_url=args.base_url, timeout=60) as client:
        login = client.post(
            "/login",
            data={"email": args.email, "password": settings.seed_password},
            follow_redirects=False,
        )
        assert login.status_code == 303, login.text
        me = client.get("/api/auth/me")
        me.raise_for_status()
        csrf = me.json()["csrf_token"]

        listing = client.get("/api/tickets")
        listing.raise_for_status()
        items = listing.json()["items"]
        assert items, "no visible ticket; run scripts/replay_sample.py first"
        ticket_id = items[0]["id"]

        processed = client.post(
            f"/api/tickets/{ticket_id}/process",
            headers={"X-CSRF-Token": csrf},
        )
        processed.raise_for_status()
        result = processed.json()

        detail = client.get(f"/api/tickets/{ticket_id}")
        detail.raise_for_status()
        payload = detail.json()

        codes = [intent["code"] for intent in result["intents"]]
        assert codes == ["not_received", "refund_request"], codes
        draft = payload["latest_draft"]
        assert draft is not None
        assert "荷物が届いていない" in draft["body"]
        assert "返金をご希望" in draft["body"]
        assert "【配送会社の記録】" in draft["body"]
        assert "【確認中の事項】" in draft["body"]
        latest_run_id = payload["agent_runs"][0]["id"]
        latest_calls = [
            call
            for call in payload["tool_calls"]
            if call["agent_run_id"] == latest_run_id
        ]
        tool_names = [call["tool_name"] for call in latest_calls]
        assert tool_names == [
            "order_get",
            "shipments_list",
            "shipment_track",
            "inventory_get",
        ], tool_names
        assert all(call["status"] == "ok" for call in latest_calls)
        evidence = draft["structured_facts"]["policy_evidence"]
        assert evidence, "policy evidence missing"
        assert all(
            item["doc_title"] != "B 租户专用退款政策" for item in evidence
        )

        html = client.get(f"/tickets/{ticket_id}")
        html.raise_for_status()
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(html.text, encoding="utf-8")
        assert EXPECTED_TEXT in html.text
        assert "运行 Agent 分析" in html.text
        assert "【配送会社の記録】" in html.text
        assert "order_get" in html.text
        assert "向承运商" not in draft["body"]
        assert "由具备退款权限" not in draft["body"]

        json_output = Path(args.json_output)
        json_output.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    print(
        json.dumps(
            {
                "ticket": payload["ticket"]["number"],
                "store": payload["store"]["code"],
                "intents": codes,
                "draft_version": draft["version"],
                "tool_calls": tool_names,
                "evidence_count": len(evidence),
                "html_artifact": str(output),
                "json_artifact": str(json_output),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()