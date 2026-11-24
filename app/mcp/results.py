from __future__ import annotations

import json

from mcp import types


def error_payload(
    error: str,
    *,
    policy_reason: str | None = None,
    detail: str | None = None,
) -> dict:
    payload: dict = {"ok": False, "error": error}
    if policy_reason:
        payload["policy_reason"] = policy_reason
    if detail:
        payload["detail"] = detail
    return payload


def server_result(payload: dict, is_error: bool = False) -> types.ServerResult:
    return types.ServerResult(
        types.CallToolResult(
            content=[
                types.TextContent(
                    type="text", text=json.dumps(payload, ensure_ascii=False)
                )
            ],
            structuredContent=payload,
            isError=is_error,
        )
    )