from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx

from app.config import get_settings
from app.security import sign_hmac

SAMPLE_TEXT = (
    "配送状況では配達済みになっていますが、荷物が届いていません。"
    "返金を申請したいです。"
)


def parse_args() -> argparse.Namespace:
    settings = get_settings()
    parser = argparse.ArgumentParser(description="Replay the JP 'delivered but not received' sample")
    parser.add_argument("--base-url", default=settings.app_base_url)
    parser.add_argument("--account", default="a-jp-01-amazon")
    parser.add_argument("--external-message-id", default="JP-DEMO-20261122-001")
    parser.add_argument("--buyer", default="buyer-jp-001")
    parser.add_argument("--buyer-name", default="山田 花子")
    parser.add_argument("--text", default=SAMPLE_TEXT)
    parser.add_argument("--order", action="append", default=None)
    parser.add_argument(
        "--concurrency",
        type=int,
        default=1,
        help="send the same payload N times concurrently to demonstrate idempotent dedup",
    )
    args = parser.parse_args()
    if args.order is None:
        args.order = ["A-JP-1001"]
    return args


async def send(client: httpx.AsyncClient, args: argparse.Namespace, secret: str, body: bytes):
    signature = sign_hmac(secret, body)
    response = await client.post(
        f"/api/inbound/{args.account}",
        content=body,
        headers={"content-type": "application/json", "X-Channel-Signature": signature},
    )
    return response.status_code, response.json()


async def main() -> None:
    args = parse_args()
    settings = get_settings()
    secret = f"{settings.test_channel_secret}-{args.account}"
    payload = {
        "external_message_id": args.external_message_id,
        "buyer_external_id": args.buyer,
        "buyer_display_name": args.buyer_name,
        "text": args.text,
        "order_candidates": args.order,
        # These fields must have no authorization effect; the channel account wins.
        "tenant_id": "forged-tenant",
        "store_id": "forged-store",
        "employee_id": "forged-employee",
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    async with httpx.AsyncClient(base_url=args.base_url, timeout=30) as client:
        results = await asyncio.gather(
            *[send(client, args, secret, body) for _ in range(max(1, args.concurrency))]
        )

    ticket_ids = {result[1].get("ticket_id") for result in results}
    output = {
        "requests": len(results),
        "status_codes": sorted({result[0] for result in results}),
        "unique_ticket_ids": sorted(ticket_ids),
        "duplicates": sorted({bool(result[1].get("duplicate")) for result in results}),
        "ticket_url": f"{args.base_url}{results[0][1].get('ticket_url', '')}",
        "original_text": args.text,
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())