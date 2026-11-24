from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from app.config import Settings
from app.security import create_context_token, decode_context_token

BUSINESS_TOOL_NAMES = {
    "orders_recent_for_buyer",
    "order_get",
    "shipments_list",
    "shipment_track",
    "inventory_get",
}

TRAINING_TOOL_NAMES = {
    "training_case_get",
    "training_sop_get",
}


@dataclass
class RunContext:
    tenant_id: uuid.UUID
    store_ids: set[uuid.UUID]
    employee_id: str
    role: str
    allowed_tools: set[str]
    ticket_id: str | None = None
    agent_run_id: str | None = None
    graph: str = "business"


def run_context_token(
    settings: Settings, context: RunContext, ttl_seconds: int = 900
) -> str:
    payload = {
        "tenant_id": str(context.tenant_id),
        "store_ids": sorted(str(store_id) for store_id in context.store_ids),
        "employee_id": context.employee_id,
        "role": context.role,
        "allowed_tools": sorted(context.allowed_tools),
        "ticket_id": context.ticket_id,
        "agent_run_id": context.agent_run_id,
        "graph": context.graph,
    }
    return create_context_token(
        settings.mcp_context_secret, payload, ttl_seconds=ttl_seconds
    )


def decode_run_context(settings: Settings, token: str) -> RunContext:
    payload = decode_context_token(settings.mcp_context_secret, token)
    store_ids = {uuid.UUID(value) for value in payload.get("store_ids", [])}
    if not store_ids:
        raise ValueError("run context has no store scope")
    return RunContext(
        tenant_id=uuid.UUID(payload["tenant_id"]),
        store_ids=store_ids,
        employee_id=str(payload["employee_id"]),
        role=str(payload["role"]),
        allowed_tools=set(payload.get("allowed_tools", [])),
        ticket_id=payload.get("ticket_id"),
        agent_run_id=payload.get("agent_run_id"),
        graph=str(payload.get("graph", "business")),
    )