from __future__ import annotations

import asyncio
import json
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

import anyio
from mcp import types
from mcp.client.session import ClientSession
from mcp.shared.memory import create_client_server_memory_streams

from app.adapters.commerce import CommerceAdapter, ScopeResolver
from app.config import Settings
from app.mcp.server import build_readonly_server
from app.mcp.training_server import build_training_server
from app.models import ToolCallStatus


class ToolBudgetExceeded(RuntimeError):
    pass


@dataclass
class ToolCallOutcome:
    tool_call_id: str
    tool_name: str
    arguments: dict
    status: ToolCallStatus
    duration_ms: int
    step_no: int
    data: object = None
    summary: dict = field(default_factory=dict)
    denied_reason: str | None = None


@asynccontextmanager
async def open_readonly_session(
    settings: Settings, adapter: CommerceAdapter, resolver: ScopeResolver
):
    server = build_readonly_server(settings, adapter, resolver)
    async with create_client_server_memory_streams() as (client_streams, server_streams):
        async with anyio.create_task_group() as task_group:
            task_group.start_soon(
                _run_server, server, server_streams[0], server_streams[1]
            )
            async with ClientSession(
                client_streams[0], client_streams[1]
            ) as client_session:
                await client_session.initialize()
                yield client_session
            task_group.cancel_scope.cancel()


@asynccontextmanager
async def open_training_session(
    settings: Settings, sessionmaker: async_sessionmaker[AsyncSession]
):
    server = build_training_server(settings, sessionmaker)
    async with create_client_server_memory_streams() as (client_streams, server_streams):
        async with anyio.create_task_group() as task_group:
            task_group.start_soon(
                _run_server, server, server_streams[0], server_streams[1]
            )
            async with ClientSession(
                client_streams[0], client_streams[1]
            ) as client_session:
                await client_session.initialize()
                yield client_session
            task_group.cancel_scope.cancel()


async def _run_server(server, read_stream, write_stream) -> None:
    await server.run(read_stream, write_stream, server.create_initialization_options())


def _summarize(payload: dict) -> dict:
    data = payload.get("data")
    if isinstance(data, list):
        return {"count": len(data)}
    if isinstance(data, dict):
        summary = {}
        for key in (
            "order_no",
            "tracking_no",
            "delivery_status",
            "status",
            "sku",
            "quantity",
        ):
            if key in data:
                summary[key] = data[key]
        return summary or {"keys": sorted(data)[:8]}
    return {}


class ScopedToolClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.started_at = time.monotonic()
        self.step_no = 0

    def _check_budget(self) -> None:
        if self.step_no >= self.settings.agent_max_steps:
            raise ToolBudgetExceeded("agent step budget exhausted")
        if (
            time.monotonic() - self.started_at
            > self.settings.agent_total_timeout_seconds
        ):
            raise ToolBudgetExceeded("agent time budget exhausted")

    async def call(
        self,
        session: ClientSession,
        context_token: str,
        name: str,
        arguments: dict,
    ) -> ToolCallOutcome:
        self._check_budget()
        self.step_no += 1
        tool_call_id = f"tc-{uuid.uuid4().hex[:16]}"
        started = time.monotonic()
        try:
            result = await asyncio.wait_for(
                session.call_tool(
                    name,
                    arguments,
                    meta={
                        "run_context": context_token,
                        "tool_call_id": tool_call_id,
                        "step_no": self.step_no,
                    },
                    read_timeout_seconds=None,
                ),
                timeout=self.settings.mcp_tool_timeout_seconds + 1.0,
            )
            payload = result.structuredContent
            if payload is None:
                text = result.content[0].text if result.content else "{}"
                try:
                    payload = json.loads(text)
                except json.JSONDecodeError:
                    payload = {"ok": False, "error": "invalid_tool_response"}
        except TimeoutError:
            payload = {
                "ok": False,
                "error": "timeout",
                "policy_reason": "tool_timeout",
            }
        except Exception as exc:  # noqa: BLE001 - recorded as tool failure
            payload = {"ok": False, "error": "transport_error", "detail": type(exc).__name__}

        duration_ms = int((time.monotonic() - started) * 1000)
        policy_reason = payload.get("policy_reason")
        if payload.get("ok"):
            status = ToolCallStatus.ok
        elif payload.get("error") == "timeout":
            status = ToolCallStatus.timeout
        elif policy_reason or payload.get("error") in {"forbidden", "unauthorized"}:
            status = ToolCallStatus.denied
        else:
            status = ToolCallStatus.error

        return ToolCallOutcome(
            tool_call_id=tool_call_id,
            tool_name=name,
            arguments=arguments,
            status=status,
            duration_ms=duration_ms,
            step_no=self.step_no,
            data=payload.get("data"),
            summary=_summarize(payload) if payload.get("ok") else {
                "error": payload.get("error"),
                "policy_reason": policy_reason,
            },
            denied_reason=policy_reason if status == ToolCallStatus.denied else None,
        )