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
    parser = argparse.ArgumentParser(description="Verify the workbench HTML for the sample ticket")
    parser.add_argument("--base-url", default=settings.app_base_url)
    parser.add_argument("--email", default="cs.a@example.com")
    parser.add_argument("--output", default=str(ROOT / "artifacts" / "stage1-ticket.html"))
    args = parser.parse_args()

    with httpx.Client(base_url=args.base_url, timeout=30) as client:
        login = client.post(
            "/login",
            data={"email": args.email, "password": settings.seed_password},
            follow_redirects=False,
        )
        assert login.status_code == 303, login.text

        listing = client.get("/api/tickets")
        listing.raise_for_status()
        items = listing.json()["items"]
        assert items, "no visible ticket; run scripts/replay_sample.py first"

        detail = client.get(f"/tickets/{items[0]['id']}")
        detail.raise_for_status()
        html = detail.text
        assert EXPECTED_TEXT in html, "full Japanese original text missing"
        assert "中文译文" in html

        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(html, encoding="utf-8")

    print(
        json.dumps(
            {
                "ticket": items[0]["number"],
                "store": items[0]["store"]["code"],
                "visible_tickets": len(items),
                "japanese_text_present": True,
                "artifact": str(output),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()