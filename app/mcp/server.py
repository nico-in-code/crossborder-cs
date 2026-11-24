from __future__ import annotations

import asyncio
import uuid

from mcp import types
from mcp.server.lowlevel import Server

from app.adapters.commerce import CommerceAdapter, ScopeResolver
from app.config import Settings
from app.mcp.context import decode_run_context
from app.mcp.results import error_payload as _error
from app.mcp.results import server_result as _server_result
from app.mcp.tools_readonly import TOOL_LIST, TOOL_SPECS, validate_arguments


class MCPScopeDenied(Exception):
    pass


def build_readonly_server(
    settings: Settings, adapter: CommerceAdapter, resolver: ScopeResolver
) -> Server:
    server: Server = Server("crossborder-cs-readonly")

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return TOOL_LIST

    async def _dispatch(
        name: str, arguments: dict, context, tool_call_id: str
    ) -> dict:
        if name == "orders_recent_for_buyer":
            limit = int(arguments.get("limit", 5))
            data = await adapter.recent_orders(
                context.tenant_id,
                context.store_ids,
                arguments["buyer_external_id"],
                max(1, min(limit, 10)),
            )
            return {"ok": True, "data": data, "tool_call_id": tool_call_id}

        if name == "order_get":
            order_no = arguments["order_no"]
            if not await resolver.order_in_scope(
                context.tenant_id, context.store_ids, order_no
            ):
                return _error(
                    "not_found", policy_reason="resource_out_of_scope"
                )
            order = await adapter.get_order(
                context.tenant_id, context.store_ids, order_no
            )
            if order is None:
                return _error("not_found")
            return {"ok": True, "data": order, "tool_call_id": tool_call_id}

        if name == "shipments_list":
            try:
                order_id = uuid.UUID(arguments["order_id"])
            except ValueError:
                return _error("invalid_arguments", detail="order_id must be a UUID")
            if not await resolver.order_id_in_scope(
                context.tenant_id, context.store_ids, order_id
            ):
                return _error(
                    "not_found", policy_reason="resource_out_of_scope"
                )
            shipments = await adapter.list_shipments(
                context.tenant_id, context.store_ids, order_id
            )
            return {
                "ok": True,
                "data": shipments,
                "tool_call_id": tool_call_id,
            }

        if name == "shipment_track":
            tracking_no = arguments["tracking_no"]
            if not await resolver.tracking_in_scope(
                context.tenant_id, context.store_ids, tracking_no
            ):
                return _error(
                    "not_found", policy_reason="resource_out_of_scope"
                )
            shipment = await adapter.track_shipment(
                context.tenant_id, context.store_ids, tracking_no
            )
            if shipment is None:
                return _error("not_found")
            return {
                "ok": True,
                "data": shipment,
                "tool_call_id": tool_call_id,
            }

        if name == "inventory_get":
            sku = arguments["sku"]
            if not await resolver.sku_in_scope(
                context.tenant_id, context.store_ids, sku
            ):
                return _error(
                    "not_found", policy_reason="resource_out_of_scope"
                )
            inventory = await adapter.get_inventory(
                context.tenant_id, context.store_ids, sku
            )
            if inventory is None:
                return _error("not_found")
            return {
                "ok": True,
                "data": inventory,
                "tool_call_id": tool_call_id,
            }

        return _error("unknown_tool")

    async def handle_call(req: types.CallToolRequest) -> types.ServerResult:
        name = req.params.name
        arguments = req.params.arguments or {}
        meta = req.params.meta
        token = getattr(meta, "run_context", None) if meta else None
        tool_call_id = str(getattr(meta, "tool_call_id", "")) if meta else ""
        if not token:
            return _server_result(
                _error("unauthorized", policy_reason="missing_run_context"),
                is_error=True,
            )
        try:
            context = decode_run_context(settings, str(token))
        except ValueError as exc:
            return _server_result(
                _error(
                    "unauthorized",
                    policy_reason="invalid_run_context",
                    detail=str(exc),
                ),
                is_error=True,
            )

        if context.role not in {"agent", "supervisor"}:
            return _server_result(
                _error("forbidden", policy_reason="role_not_allowed"),
                is_error=True,
            )
        if name not in context.allowed_tools:
            return _server_result(
                _error("forbidden", policy_reason="tool_not_allowlisted"),
                is_error=True,
            )
        spec = TOOL_SPECS.get(name)
        if spec is None:
            return _server_result(_error("unknown_tool"), is_error=True)
        validation_error = validate_arguments(spec, arguments)
        if validation_error:
            return _server_result(
                _error("invalid_arguments", detail=validation_error),
                is_error=True,
            )

        try:
            payload = await asyncio.wait_for(
                _dispatch(name, arguments, context, tool_call_id),
                timeout=settings.mcp_tool_timeout_seconds,
            )
        except TimeoutError:
            payload = _error("timeout", policy_reason="tool_timeout")
        except Exception as exc:  # noqa: BLE001 - surfaced as structured tool failure
            payload = _error("upstream_error", detail=type(exc).__name__)
        return _server_result(payload, is_error=not payload.get("ok", False))

    server.request_handlers[types.CallToolRequest] = handle_call
    return server