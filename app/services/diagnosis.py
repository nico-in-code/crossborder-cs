from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.adapters.commerce import CommerceAdapter, ScopeResolver
from app.agents.business import AgentRuntime, run_business_queries
from app.config import Settings
from app.mcp.context import BUSINESS_TOOL_NAMES, RunContext, run_context_token
from app.models import (
    AgentRun,
    Draft,
    Employee,
    EmployeeRole,
    Followup,
    FollowupStatus,
    KnowledgeStatus,
    Message,
    MessageDirection,
    RunStatus,
    Sop,
    SopVersion,
    StoreGrant,
    Ticket,
    TicketEvent,
    TicketStatus,
    ToolCall,
    ToolCallStatus,
)
from app.services.permission import get_ticket_for_employee
from app.trace import log_event, new_trace_id, utcnow

BUSINESS_GAP_TOKENS = ("物流", "运单", "订单", "超时", "查询", "承运")
HARD_TOOL_STATUSES = {
    ToolCallStatus.denied,
    ToolCallStatus.timeout,
    ToolCallStatus.error,
}


def _refund_requested(intents: list) -> bool:
    return any(intent.get("code") == "refund_request" for intent in intents or [])


def _tool_evidence(row: ToolCall, source: str) -> dict:
    return {
        "source": source,
        "tool_call_id": row.tool_call_id,
        "tool_name": row.tool_name,
        "status": row.result_status.value,
        "step_no": row.step_no,
        "summary": row.result_summary,
        "denied_reason": row.denied_reason,
    }


def _outcome_evidence(outcome: dict, source: str) -> dict:
    return {
        "source": source,
        "tool_call_id": outcome["tool_call_id"],
        "tool_name": outcome["tool_name"],
        "status": outcome["status"],
        "step_no": outcome["step_no"],
        "summary": outcome["summary"],
        "denied_reason": outcome.get("denied_reason"),
    }


def _carrier_phrase(order: dict | None, shipment: dict | None) -> str:
    order = order or {}
    shipment = shipment or {}
    parts = []
    if order.get("order_no"):
        parts.append(f"订单 {order['order_no']}")
    if shipment.get("tracking_no"):
        parts.append(
            f"运单 {shipment['tracking_no']}"
            f"（{shipment.get('carrier') or '承运商未知'}，"
            f"状态 {shipment.get('delivery_status') or 'unknown'}）"
        )
    return "，".join(parts) if parts else "订单与运单事实"


def _next_step_text(
    path: str,
    reasons: list[str],
    order: dict | None,
    shipment: dict | None,
    gaps: list[str],
    requires_manual_review: bool,
) -> str:
    carrier = _carrier_phrase(order, shipment)
    if path == "reuse":
        return (
            f"复用现有证据：{carrier}。按 SOP 向承运商发起调查并记录查询编号；"
            "调查完成前不承诺退款；退款由具备退款权限的客服跟进。"
        )
    if path == "requery":
        text = (
            f"已重新查询并采用最新事实：{carrier}。按 SOP 向承运商发起调查并记录查询编号；"
            "调查完成前不承诺退款；退款由具备退款权限的客服跟进。"
        )
        if requires_manual_review:
            text += "重查仍有缺口，发送对外承诺前需人工复核。"
        elif gaps:
            text += f"待办缺口：{'；'.join(gaps)}。"
        return text
    return (
        f"转人工处理：{'；'.join(reasons) or '证据不足'}。"
        "补齐证据前不发送对外承诺，由人工核对订单归属、政策依据和退款权限。"
    )


async def diagnose_ticket(
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    commerce: CommerceAdapter,
    resolver: ScopeResolver,
    actor_employee_id: uuid.UUID,
    ticket_id: uuid.UUID,
) -> dict:
    trace_id = new_trace_id()

    async with sessionmaker() as session:
        employee = await session.get(Employee, actor_employee_id)
        if employee is None:
            raise HTTPException(status_code=401, detail="actor not found")
        ticket = await get_ticket_for_employee(session, employee, ticket_id, "read")
        if ticket.status is TicketStatus.closed:
            raise HTTPException(status_code=409, detail="ticket already closed")

        inbound_messages = list(
            (
                await session.scalars(
                    select(Message)
                    .where(
                        Message.ticket_id == ticket.id,
                        Message.direction == MessageDirection.inbound,
                    )
                    .order_by(Message.created_at.desc())
                )
            ).all()
        )
        latest_draft = await session.scalar(
            select(Draft)
            .where(Draft.ticket_id == ticket.id)
            .order_by(Draft.version.desc())
            .limit(1)
        )
        master_run = await session.scalar(
            select(AgentRun)
            .where(AgentRun.ticket_id == ticket.id, AgentRun.graph == "master")
            .order_by(AgentRun.created_at.desc())
            .limit(1)
        )
        master_calls = (
            list(
                (
                    await session.scalars(
                        select(ToolCall)
                        .where(ToolCall.agent_run_id == master_run.id)
                        .order_by(ToolCall.step_no)
                    )
                ).all()
            )
            if master_run is not None
            else []
        )
        sop_row = (
            await session.execute(
                select(SopVersion, Sop)
                .join(Sop, Sop.id == SopVersion.sop_id)
                .where(
                    SopVersion.tenant_id == ticket.tenant_id,
                    Sop.tenant_id == ticket.tenant_id,
                    SopVersion.status == KnowledgeStatus.approved,
                )
                .order_by(SopVersion.approved_at.desc().nulls_last(), SopVersion.version.desc())
                .limit(1)
            )
        ).first()

        refund_case = _refund_requested(ticket.intents)
        preferred_owner = None
        owner_from_candidate = False
        candidate_id = ticket.next_owner_id or ticket.assignee_id
        if candidate_id is not None:
            preferred_owner = await session.get(Employee, candidate_id)
            if preferred_owner is not None and not preferred_owner.active:
                preferred_owner = None
            elif preferred_owner is not None and not refund_case:
                owner_from_candidate = True
        if preferred_owner is None or refund_case:
            owner_row = await session.scalar(
                select(Employee)
                .join(StoreGrant, StoreGrant.employee_id == Employee.id)
                .where(
                    Employee.tenant_id == ticket.tenant_id,
                    Employee.active.is_(True),
                    Employee.role.in_([EmployeeRole.agent, EmployeeRole.supervisor]),
                    Employee.refund_authority.is_(True),
                    StoreGrant.tenant_id == ticket.tenant_id,
                    StoreGrant.store_id == ticket.store_id,
                )
                .order_by(Employee.email)
                .limit(1)
            )
            if owner_row is not None:
                preferred_owner = owner_row

        intents = list(ticket.intents or [])
        draft_facts = (latest_draft.structured_facts or {}) if latest_draft else {}
        draft_gaps = list(draft_facts.get("gaps") or [])
        business_calls = [
            call for call in master_calls if call.tool_name in BUSINESS_TOOL_NAMES
        ]
        denied_calls = [
            call for call in business_calls if call.result_status is ToolCallStatus.denied
        ]
        failed_calls = [
            call
            for call in business_calls
            if call.result_status in {ToolCallStatus.timeout, ToolCallStatus.error}
        ]
        business_gaps = [
            gap
            for gap in draft_gaps
            if any(token in gap for token in BUSINESS_GAP_TOKENS)
        ]
        policy_gaps = [
            gap for gap in draft_gaps if "政策" in gap or "FAQ" in gap
        ]
        has_policy_evidence = bool((latest_draft.evidence if latest_draft else []) or [])
        sop_version, sop = sop_row if sop_row is not None else (None, None)

        reasons: list[str] = []
        path: str
        if master_run is None:
            path = "manual"
            reasons.append("尚未运行主分析 Agent，缺少工具结果")
        elif latest_draft is None:
            path = "manual"
            reasons.append("缺少草稿与结构化证据")
        elif not inbound_messages:
            path = "manual"
            reasons.append("工单没有买家来信")
        elif denied_calls:
            path = "manual"
            reasons.append(
                "工具调用被拒绝："
                + "，".join(
                    f"{call.tool_name}/{call.denied_reason or 'denied'}"
                    for call in denied_calls
                )
            )
        elif sop_version is None:
            path = "manual"
            reasons.append("租户没有已批准的 SOP 版本")
        elif failed_calls or business_gaps:
            path = "requery"
            reasons.append("主分析的工具结果缺失或已过期，需要重查业务事实")
        elif policy_gaps and not has_policy_evidence:
            path = "manual"
            reasons.append("缺少适用政策证据，MCP 业务工具无法补齐")
        else:
            path = "reuse"
            reasons.append("主分析工具结果完整，政策证据可用，可复用现有事实")

        ticket_snapshot = {
            "id": ticket.id,
            "tenant_id": ticket.tenant_id,
            "store_id": ticket.store_id,
            "order_candidates": list(ticket.order_candidates or []),
            "intents": intents,
        }
        message_snapshot = [
            {
                "id": str(message.id),
                "trace_id": message.trace_id,
                "language": message.original_language,
                "translation_state": message.translation_state.value,
                "created_at": message.created_at.isoformat(),
            }
            for message in inbound_messages[:5]
        ]
        draft_snapshot = (
            {
                "draft_id": str(latest_draft.id),
                "version": latest_draft.version,
                "content_hash": latest_draft.content_hash,
                "gaps": draft_gaps,
                "to_verify": [
                    item.get("code") for item in draft_facts.get("to_verify", [])
                ],
                "policy_evidence": [
                    {
                        "doc_title": item.get("doc_title"),
                        "version": item.get("version"),
                        "rerank_score": item.get("rerank_score"),
                    }
                    for item in (latest_draft.evidence or [])
                ],
            }
            if latest_draft is not None
            else None
        )
        sop_snapshot = (
            {
                "code": sop.code,
                "title": sop.title,
                "version": sop_version.version,
                "checksum": sop_version.checksum,
                "body": sop_version.body,
            }
            if sop is not None and sop_version is not None
            else None
        )
        owner_snapshot = (
            {
                "id": str(preferred_owner.id),
                "display_name": preferred_owner.display_name,
                "reason": (
                    "退款诉求需要具备退款权限且已授权该店铺的客服"
                    if refund_case
                    else (
                        "沿用当前负责人"
                        if owner_from_candidate
                        else "已授权该店铺的客服"
                    )
                ),
            }
            if preferred_owner is not None
            else None
        )
        employee_role = employee.role.value
        store_ids = {ticket.store_id}
        conversation_buyer = await session.scalar(
            select(Message.buyer_external_id)
            .where(
                Message.ticket_id == ticket.id,
                Message.direction == MessageDirection.inbound,
            )
            .order_by(Message.created_at.desc())
            .limit(1)
        )

    async with sessionmaker() as session:
        run = AgentRun(
            tenant_id=ticket_snapshot["tenant_id"],
            ticket_id=ticket_snapshot["id"],
            thread_id=f"diagnose:{ticket_snapshot['id']}",
            graph="diagnose",
            status=RunStatus.running,
            state_summary={},
            trace_id=trace_id,
        )
        session.add(run)
        await session.flush()
        run_id = run.id
        await session.commit()

    requery_outcomes: list[dict] = []
    requery_business: dict = {}
    requery_gaps: list[str] = []
    requery_failed = False
    requires_manual_review = path == "manual"

    if path == "requery":
        context = RunContext(
            tenant_id=ticket_snapshot["tenant_id"],
            store_ids=store_ids,
            employee_id=str(actor_employee_id),
            role=employee_role,
            allowed_tools=BUSINESS_TOOL_NAMES,
            ticket_id=str(ticket_snapshot["id"]),
            agent_run_id=str(run_id),
            graph="diagnose",
        )
        runtime = AgentRuntime(
            settings=settings,
            sessionmaker=sessionmaker,
            commerce=commerce,
            resolver=resolver,
            context_token=run_context_token(settings, context),
            tenant_id=ticket_snapshot["tenant_id"],
            store_id=ticket_snapshot["store_id"],
            ticket_id=str(ticket_snapshot["id"]),
            buyer_external_id=conversation_buyer or "",
        )
        try:
            result = await run_business_queries(
                runtime, ticket_snapshot["order_candidates"]
            )
        except Exception as exc:  # noqa: BLE001 - recorded on the diagnosis run
            requery_failed = True
            result = {
                "business": {},
                "tool_outcomes": [],
                "gaps": ["重新查询执行失败，需人工处理"],
            }
            reasons.append(f"重新查询执行失败（{type(exc).__name__}）")
        requery_outcomes = result["tool_outcomes"]
        requery_business = result["business"]
        requery_gaps = list(result["gaps"])
        hard_outcomes = [
            outcome
            for outcome in requery_outcomes
            if outcome["status"] in {"denied", "timeout", "error"}
        ]
        if hard_outcomes or requery_failed or not requery_outcomes:
            path = "manual"
            requires_manual_review = True
            if hard_outcomes:
                reasons.append(
                    "重新查询仍失败："
                    + "，".join(
                        f"{outcome['tool_name']}/{outcome['status']}"
                        for outcome in hard_outcomes
                    )
                )
            elif not requery_failed:
                reasons.append("重新查询没有返回任何工具结果，需人工处理")
        else:
            requires_manual_review = False
            reasons.extend(requery_gaps)

    if path == "reuse":
        order = None
        shipment = None
        carrier = draft_facts.get("carrier_records") or {}
        if carrier:
            shipment = carrier
            order = {"order_no": carrier.get("order_no")}
    elif path == "requery":
        order = requery_business.get("order")
        shipment = requery_business.get("shipment")
    else:
        order = None
        shipment = None

    gaps = requery_gaps if requery_outcomes or requery_gaps else list(draft_gaps)

    recommendation = {
        "next_step": _next_step_text(
            path,
            reasons,
            order,
            shipment,
            gaps,
            requires_manual_review,
        ),
        "next_owner": owner_snapshot,
        "next_follow_up_at": (utcnow() + timedelta(hours=24)).isoformat(),
        "requires_manual_review": requires_manual_review,
    }

    tool_evidence = [_tool_evidence(call, "master") for call in business_calls] + [
        _outcome_evidence(outcome, "requery") for outcome in requery_outcomes
    ]

    state_summary = {
        "path": path,
        "reasons": reasons,
        "requires_manual_review": requires_manual_review,
        "gaps": gaps,
        "evidence": {
            "messages": message_snapshot,
            "draft": draft_snapshot,
            "sop": sop_snapshot,
            "tool_calls": tool_evidence,
        },
        "recommendation": recommendation,
    }

    async with sessionmaker() as session:
        run = await session.get(AgentRun, run_id)
        if run is None:
            raise HTTPException(status_code=500, detail="diagnosis run vanished")
        for outcome in requery_outcomes:
            session.add(
                ToolCall(
                    tenant_id=ticket_snapshot["tenant_id"],
                    agent_run_id=run.id,
                    tool_call_id=outcome["tool_call_id"],
                    tool_name=outcome["tool_name"],
                    arguments=outcome["arguments"],
                    result_status=ToolCallStatus(outcome["status"]),
                    result_summary=outcome["summary"],
                    duration_ms=outcome["duration_ms"],
                    step_no=outcome["step_no"],
                    denied_reason=outcome.get("denied_reason"),
                )
            )
        run.status = RunStatus.completed
        run.state_summary = state_summary
        run.updated_at = utcnow()
        session.add(
            TicketEvent(
                tenant_id=ticket_snapshot["tenant_id"],
                ticket_id=ticket_snapshot["id"],
                kind="diagnosis_completed",
                actor_id=actor_employee_id,
                trace_id=trace_id,
                payload={
                    "diagnosis_id": str(run.id),
                    "path": path,
                    "requires_manual_review": requires_manual_review,
                    "tool_calls": len(requery_outcomes),
                },
            )
        )
        await session.commit()
        diagnosis_id = run.id

    log_event(
        "diagnosis_completed",
        trace_id=trace_id,
        ticket_id=str(ticket_snapshot["id"]),
        path=path,
        requery_tools=len(requery_outcomes),
        requires_manual_review=requires_manual_review,
    )

    return {
        "ticket_id": str(ticket_snapshot["id"]),
        "diagnosis_id": str(diagnosis_id),
        "trace_id": trace_id,
        "path": path,
        "reasons": reasons,
        "gaps": gaps,
        "requires_manual_review": requires_manual_review,
        "evidence": state_summary["evidence"],
        "recommendation": recommendation,
    }


async def apply_diagnosis(
    session: AsyncSession,
    employee: Employee,
    ticket_id: uuid.UUID,
    diagnosis_id: str | None,
    next_step: str,
    next_owner_id: str | None,
    next_follow_up_at: datetime | None,
    note: str | None = None,
) -> dict:
    ticket = await get_ticket_for_employee(session, employee, ticket_id, "read")
    ticket = await session.get(Ticket, ticket.id, with_for_update=True)
    if ticket is None:
        raise HTTPException(status_code=404, detail="ticket not found")
    if ticket.status is TicketStatus.closed:
        raise HTTPException(status_code=409, detail="ticket already closed")
    if not next_step.strip():
        raise HTTPException(status_code=422, detail="next_step is required")

    if diagnosis_id:
        try:
            run_id = uuid.UUID(diagnosis_id)
        except ValueError:
            raise HTTPException(status_code=422, detail="diagnosis_id must be a UUID")
        diagnosis = await session.get(AgentRun, run_id)
        if (
            diagnosis is None
            or diagnosis.ticket_id != ticket.id
            or diagnosis.graph != "diagnose"
        ):
            raise HTTPException(status_code=404, detail="diagnosis not found")
    else:
        diagnosis = await session.scalar(
            select(AgentRun)
            .where(AgentRun.ticket_id == ticket.id, AgentRun.graph == "diagnose")
            .order_by(AgentRun.created_at.desc())
            .limit(1)
        )
        if diagnosis is None:
            raise HTTPException(status_code=409, detail="run diagnosis first")

    owner = None
    if next_owner_id:
        try:
            owner_id = uuid.UUID(next_owner_id)
        except ValueError:
            raise HTTPException(status_code=422, detail="next_owner_id must be a UUID")
        owner = await session.get(Employee, owner_id)
        if (
            owner is None
            or owner.tenant_id != ticket.tenant_id
            or not owner.active
        ):
            raise HTTPException(status_code=404, detail="next owner not found")
        grant = await session.scalar(
            select(StoreGrant.id).where(
                StoreGrant.tenant_id == ticket.tenant_id,
                StoreGrant.employee_id == owner.id,
                StoreGrant.store_id == ticket.store_id,
            )
        )
        if grant is None:
            raise HTTPException(status_code=404, detail="next owner not found")
        if owner.id != employee.id:
            await get_ticket_for_employee(session, employee, ticket.id, "assign")

    follow_up = next_follow_up_at
    if follow_up is not None and follow_up.tzinfo is None:
        follow_up = follow_up.replace(tzinfo=timezone.utc)

    ticket.next_step = next_step.strip()
    if owner is not None:
        ticket.next_owner_id = owner.id
        if ticket.assignee_id is None:
            ticket.assignee_id = owner.id
    if follow_up is not None:
        ticket.next_follow_up_at = follow_up
    ticket.updated_at = utcnow()

    refund = _refund_requested(ticket.intents)
    kind = "refund" if refund else "reply"
    followup = await session.scalar(
        select(Followup).where(
            Followup.ticket_id == ticket.id,
            Followup.kind == kind,
            Followup.status.in_([FollowupStatus.pending, FollowupStatus.in_progress]),
        )
    )
    if followup is None:
        followup = Followup(
            tenant_id=ticket.tenant_id,
            ticket_id=ticket.id,
            kind=kind,
            status=FollowupStatus.pending,
            owner_id=owner.id if owner is not None else ticket.assignee_id,
            due_at=follow_up or utcnow(),
        )
        session.add(followup)
        await session.flush()
    else:
        if owner is not None:
            followup.owner_id = owner.id
        if follow_up is not None:
            followup.due_at = follow_up

    path = (diagnosis.state_summary or {}).get("path")
    session.add(
        TicketEvent(
            tenant_id=ticket.tenant_id,
            ticket_id=ticket.id,
            kind="diagnosis_applied",
            actor_id=employee.id,
            trace_id=diagnosis.trace_id,
            payload={
                "diagnosis_id": str(diagnosis.id),
                "path": path,
                "next_step": ticket.next_step,
                "next_owner_id": str(owner.id) if owner is not None else None,
                "next_follow_up_at": (
                    follow_up.isoformat() if follow_up is not None else None
                ),
                "followup_id": str(followup.id),
                "note": note,
            },
        )
    )
    await session.commit()

    return {
        "ticket_id": str(ticket.id),
        "diagnosis_id": str(diagnosis.id),
        "path": path,
        "next_step": ticket.next_step,
        "next_owner": (
            {"id": str(owner.id), "display_name": owner.display_name}
            if owner is not None
            else None
        ),
        "next_follow_up_at": (
            follow_up.isoformat() if follow_up is not None else None
        ),
        "followup_id": str(followup.id),
    }