from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.adapters.commerce import CommerceAdapter, ScopeResolver
from app.config import Settings
from app.mcp.client import ScopedToolClient, open_readonly_session
from app.models import ToolCallStatus


@dataclass
class AgentRuntime:
    settings: Settings
    sessionmaker: async_sessionmaker[AsyncSession]
    commerce: CommerceAdapter
    resolver: ScopeResolver
    context_token: str
    tenant_id: uuid.UUID
    store_id: uuid.UUID
    ticket_id: str
    buyer_external_id: str


def _outcome_dict(outcome) -> dict:
    return {
        "tool_call_id": outcome.tool_call_id,
        "tool_name": outcome.tool_name,
        "arguments": outcome.arguments,
        "status": outcome.status.value,
        "duration_ms": outcome.duration_ms,
        "step_no": outcome.step_no,
        "summary": outcome.summary,
        "denied_reason": outcome.denied_reason,
    }


async def run_business_queries(
    runtime: AgentRuntime, order_candidates: list[str]
) -> dict:
    tool_outcomes: list[dict] = []
    gaps: list[str] = []
    facts: dict = {
        "order": None,
        "shipments": [],
        "shipment": None,
        "inventory": None,
    }

    async with open_readonly_session(
        runtime.settings, runtime.commerce, runtime.resolver
    ) as mcp_session:
        client = ScopedToolClient(runtime.settings)

        if order_candidates:
            order_outcome = await client.call(
                mcp_session,
                runtime.context_token,
                "order_get",
                {"order_no": order_candidates[0]},
            )
            tool_outcomes.append(_outcome_dict(order_outcome))
            if order_outcome.status is not ToolCallStatus.ok:
                reason = order_outcome.denied_reason or (
                    "timeout" if order_outcome.status is ToolCallStatus.timeout else "error"
                )
                gaps.append(f"订单归属或详情查询未完成：{reason}（{order_candidates[0]}）")
                return {
                    "business": facts,
                    "tool_outcomes": tool_outcomes,
                    "gaps": gaps,
                }
            facts["order"] = order_outcome.data
        else:
            recent_outcome = await client.call(
                mcp_session,
                runtime.context_token,
                "orders_recent_for_buyer",
                {"buyer_external_id": runtime.buyer_external_id, "limit": 3},
            )
            tool_outcomes.append(_outcome_dict(recent_outcome))
            if recent_outcome.status is not ToolCallStatus.ok:
                gaps.append("买家近期订单查询未完成，需人工核对订单归属")
                return {
                    "business": facts,
                    "tool_outcomes": tool_outcomes,
                    "gaps": gaps,
                }
            recent_orders = recent_outcome.data or []
            if not recent_orders:
                gaps.append("未找到该买家的近期订单，需人工核对订单归属")
                return {
                    "business": facts,
                    "tool_outcomes": tool_outcomes,
                    "gaps": gaps,
                }
            facts["order"] = recent_orders[0]
            gaps.append("订单由买家近期订单推断，需人工确认工单与订单归属")

        order = facts["order"] or {}
        order_id = order.get("order_id")
        if not order_id:
            gaps.append("订单响应缺少订单 ID，无法继续查询运单")
            return {"business": facts, "tool_outcomes": tool_outcomes, "gaps": gaps}

        shipments_outcome = await client.call(
            mcp_session,
            runtime.context_token,
            "shipments_list",
            {"order_id": str(order_id)},
        )
        tool_outcomes.append(_outcome_dict(shipments_outcome))
        if shipments_outcome.status is ToolCallStatus.ok:
            facts["shipments"] = shipments_outcome.data or []
            if facts["shipments"]:
                facts["shipment"] = facts["shipments"][0]
            else:
                gaps.append("订单存在但未找到关联运单")
        else:
            gaps.append("关联运单查询失败，物流事实缺失")

        if facts["shipment"]:
            track_outcome = await client.call(
                mcp_session,
                runtime.context_token,
                "shipment_track",
                {"tracking_no": facts["shipment"]["tracking_no"]},
            )
            tool_outcomes.append(_outcome_dict(track_outcome))
            if track_outcome.status is ToolCallStatus.ok:
                facts["shipment"] = track_outcome.data
            else:
                gaps.append("物流轨迹查询超时或失败，保留已有运单信息并标记待补查")

        items = order.get("items") or []
        if items and isinstance(items[0], dict) and items[0].get("sku"):
            inventory_outcome = await client.call(
                mcp_session,
                runtime.context_token,
                "inventory_get",
                {"sku": str(items[0]["sku"])},
            )
            tool_outcomes.append(_outcome_dict(inventory_outcome))
            if inventory_outcome.status is ToolCallStatus.ok:
                facts["inventory"] = inventory_outcome.data
            else:
                gaps.append("库存查询未完成（非阻塞，已记录）")

    return {"business": facts, "tool_outcomes": tool_outcomes, "gaps": gaps}