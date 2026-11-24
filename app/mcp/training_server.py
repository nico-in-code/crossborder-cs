from __future__ import annotations

import asyncio
import uuid

from mcp import types
from mcp.server.lowlevel import Server
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.mcp.context import decode_run_context
from app.mcp.results import error_payload, server_result
from app.mcp.tools_readonly import validate_arguments
from app.mcp.tools_training import TRAINING_TOOL_LIST, TRAINING_TOOL_SPECS
from app.models import KnowledgeStatus, Sop, SopVersion, TrainingCase, TrainingStatus

TRAINING_ROLES = {"agent", "supervisor"}


def build_training_server(
    settings: Settings, sessionmaker: async_sessionmaker[AsyncSession]
) -> Server:
    server: Server = Server("crossborder-cs-training")

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return TRAINING_TOOL_LIST

    async def _dispatch(
        name: str, arguments: dict, context, tool_call_id: str
    ) -> dict:
        try:
            case_id = uuid.UUID(arguments["case_id"])
        except ValueError:
            return error_payload("invalid_arguments", detail="case_id must be a UUID")

        async with sessionmaker() as session:
            case = await session.scalar(
                select(TrainingCase).where(
                    TrainingCase.id == case_id,
                    TrainingCase.tenant_id == context.tenant_id,
                    TrainingCase.status == TrainingStatus.approved,
                )
            )
            if case is None:
                return error_payload("not_found", policy_reason="case_out_of_scope")

            if name == "training_case_get":
                return {
                    "ok": True,
                    "data": {
                        "case_id": str(case.id),
                        "title": case.title,
                        "anonymized_content": case.anonymized_content,
                        "marketplace": case.marketplace,
                        "language": case.language,
                        "product_scope": case.product_scope,
                    },
                    "tool_call_id": tool_call_id,
                }

            if name == "training_sop_get":
                row = (
                    await session.execute(
                        select(SopVersion, Sop)
                        .join(Sop, Sop.id == SopVersion.sop_id)
                        .where(
                            SopVersion.id == case.sop_version_id,
                            SopVersion.tenant_id == context.tenant_id,
                            Sop.tenant_id == context.tenant_id,
                            SopVersion.status == KnowledgeStatus.approved,
                        )
                    )
                ).first()
                if row is None:
                    return error_payload(
                        "not_found", policy_reason="sop_not_approved"
                    )
                sop_version, sop = row
                return {
                    "ok": True,
                    "data": {
                        "sop_code": sop.code,
                        "sop_title": sop.title,
                        "version": sop_version.version,
                        "body": sop_version.body,
                        "checksum": sop_version.checksum,
                    },
                    "tool_call_id": tool_call_id,
                }

        return error_payload("unknown_tool")

    async def handle_call(req: types.CallToolRequest) -> types.ServerResult:
        name = req.params.name
        arguments = req.params.arguments or {}
        meta = req.params.meta
        token = getattr(meta, "run_context", None) if meta else None
        tool_call_id = str(getattr(meta, "tool_call_id", "")) if meta else ""
        if not token:
            return server_result(
                error_payload("unauthorized", policy_reason="missing_run_context"),
                is_error=True,
            )
        try:
            context = decode_run_context(settings, str(token))
        except ValueError as exc:
            return server_result(
                error_payload(
                    "unauthorized",
                    policy_reason="invalid_run_context",
                    detail=str(exc),
                ),
                is_error=True,
            )

        if context.graph != "training":
            return server_result(
                error_payload("forbidden", policy_reason="graph_not_training"),
                is_error=True,
            )
        if context.role not in TRAINING_ROLES:
            return server_result(
                error_payload("forbidden", policy_reason="role_not_allowed"),
                is_error=True,
            )
        if name not in context.allowed_tools:
            return server_result(
                error_payload("forbidden", policy_reason="tool_not_allowlisted"),
                is_error=True,
            )
        spec = TRAINING_TOOL_SPECS.get(name)
        if spec is None:
            return server_result(error_payload("unknown_tool"), is_error=True)
        validation_error = validate_arguments(spec, arguments)
        if validation_error:
            return server_result(
                error_payload("invalid_arguments", detail=validation_error),
                is_error=True,
            )

        try:
            payload = await asyncio.wait_for(
                _dispatch(name, arguments, context, tool_call_id),
                timeout=settings.mcp_tool_timeout_seconds,
            )
        except TimeoutError:
            payload = error_payload("timeout", policy_reason="tool_timeout")
        except Exception as exc:  # noqa: BLE001 - surfaced as structured tool failure
            payload = error_payload("upstream_error", detail=type(exc).__name__)
        return server_result(payload, is_error=not payload.get("ok", False))

    server.request_handlers[types.CallToolRequest] = handle_call
    return server