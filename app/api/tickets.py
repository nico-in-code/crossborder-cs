from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.workflow import review_workflow_snapshot
from app.deps import AuthContext, get_db, require_auth, require_csrf
from app.schemas import (
    ApprovePayload,
    AssignPayload,
    ClosePayload,
    DiagnoseApplyPayload,
    EditDraftPayload,
    FollowupPayload,
    RejectPayload,
)
from app.services.diagnosis import apply_diagnosis, diagnose_ticket
from app.services.permission import get_ticket_for_employee
from app.services.pipeline import process_ticket
from app.services.review import (
    approve_draft,
    assign_ticket,
    close_ticket,
    edit_draft,
    reject_draft,
    update_followup,
)
from app.services.ticket import get_ticket_detail, list_tickets

router = APIRouter(prefix="/api/tickets", tags=["tickets"])


@router.get("")
async def tickets(
    status: str | None = Query(default=None),
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    rows = await list_tickets(db, auth.employee, status=status)
    return {
        "items": [
            {
                "id": str(ticket.id),
                "number": ticket.number,
                "status": ticket.status.value,
                "priority": ticket.priority,
                "store": {"id": str(store.id), "code": store.code, "name": store.name},
                "intents": ticket.intents,
                "risk_flags": ticket.risk_flags,
                "summary": ticket.summary,
                "updated_at": ticket.updated_at,
            }
            for ticket, store in rows
        ]
    }


@router.get("/{ticket_id}")
async def ticket_detail(
    ticket_id: uuid.UUID,
    request: Request,
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    detail = await get_ticket_detail(db, auth.employee, ticket_id)
    ticket = detail["ticket"]
    store = detail["store"]
    conversation = detail["conversation"]
    return {
        "ticket": {
            "id": str(ticket.id),
            "number": ticket.number,
            "status": ticket.status.value,
            "priority": ticket.priority,
            "intents": ticket.intents,
            "risk_flags": ticket.risk_flags,
            "order_candidates": ticket.order_candidates,
            "summary": ticket.summary,
            "next_step": ticket.next_step,
            "next_follow_up_at": ticket.next_follow_up_at,
            "updated_at": ticket.updated_at,
            "closed_at": ticket.closed_at,
        },
        "store": {"id": str(store.id), "code": store.code, "name": store.name},
        "next_owner": (
            {
                "id": str(detail["next_owner"].id),
                "display_name": detail["next_owner"].display_name,
            }
            if detail["next_owner"]
            else None
        ),
        "owners": [
            {"id": str(owner.id), "display_name": owner.display_name}
            for owner in detail["owners"]
        ],
        "latest_diagnosis": detail["latest_diagnosis"],
        "buyer": {
            "external_id": conversation.buyer_external_id if conversation else None,
            "display_name": conversation.buyer_display_name if conversation else None,
        },
        "assignee": (
            {"id": str(detail["assignee"].id), "display_name": detail["assignee"].display_name}
            if detail["assignee"]
            else None
        ),
        "messages": [
            {
                "id": str(message.id),
                "direction": message.direction.value,
                "body_text": message.body_text,
                "original_language": message.original_language,
                "translated_text": message.translated_text,
                "translated_language": message.translated_language,
                "translation_state": message.translation_state.value,
                "trace_id": message.trace_id,
                "created_at": message.created_at,
            }
            for message in detail["messages"]
        ],
        "followups": [
            {
                "id": str(followup.id),
                "kind": followup.kind,
                "status": followup.status.value,
                "conclusion": followup.conclusion,
                "basis": followup.basis,
                "notified_at": followup.notified_at,
            }
            for followup in detail["followups"]
        ],
        "events": [
            {
                "kind": event.kind,
                "payload": event.payload,
                "trace_id": event.trace_id,
                "created_at": event.created_at,
            }
            for event in detail["events"]
        ],
        "drafts": [
            {
                "id": str(draft.id),
                "version": draft.version,
                "language": draft.language,
                "body": draft.body,
                "content_hash": draft.content_hash,
                "structured_facts": draft.structured_facts,
                "evidence": draft.evidence,
                "status": draft.status,
                "created_at": draft.created_at,
            }
            for draft in detail["drafts"]
        ],
        "latest_draft": (
            {
                "id": str(detail["latest_draft"].id),
                "version": detail["latest_draft"].version,
                "body": detail["latest_draft"].body,
                "content_hash": detail["latest_draft"].content_hash,
                "structured_facts": detail["latest_draft"].structured_facts,
                "evidence": detail["latest_draft"].evidence,
            }
            if detail["latest_draft"]
            else None
        ),
        "tool_calls": [
            {
                "agent_run_id": str(tool_call.agent_run_id),
                "tool_call_id": tool_call.tool_call_id,
                "tool_name": tool_call.tool_name,
                "arguments": tool_call.arguments,
                "status": tool_call.result_status.value,
                "summary": tool_call.result_summary,
                "duration_ms": tool_call.duration_ms,
                "step_no": tool_call.step_no,
                "denied_reason": tool_call.denied_reason,
            }
            for tool_call in detail["tool_calls"]
        ],
        "agent_runs": [
            {
                "id": str(run.id),
                "graph": run.graph,
                "status": run.status.value,
                "state_summary": run.state_summary,
                "trace_id": run.trace_id,
                "created_at": run.created_at,
            }
            for run in detail["agent_runs"]
        ],
        "reviews": [
            {
                "id": str(review.id),
                "draft_id": str(review.draft_id),
                "draft_version": review.draft_version,
                "draft_content_hash": review.draft_content_hash,
                "reviewer_id": str(review.reviewer_id),
                "decision": review.decision.value,
                "is_current": review.is_current,
                "note": review.note,
                "decided_at": review.decided_at,
            }
            for review in detail["reviews"]
        ],
        "send_tasks": [
            {
                "id": str(task.id),
                "review_id": str(task.review_id),
                "status": task.status.value,
                "idempotency_key": task.idempotency_key,
                "attempt_count": task.attempt_count,
                "last_error": task.last_error,
                "next_retry_at": task.next_retry_at,
                "payload": {
                    "draft_id": task.payload.get("draft_id"),
                    "draft_version": task.payload.get("draft_version"),
                    "content_hash": task.payload.get("content_hash"),
                },
                "created_at": task.created_at,
            }
            for task in detail["send_tasks"]
        ],
        "send_attempts": [
            {
                "id": str(attempt.id),
                "send_task_id": str(attempt.send_task_id),
                "attempt_no": attempt.attempt_no,
                "outcome": attempt.outcome.value,
                "external_send_id": attempt.external_send_id,
                "response_code": attempt.response_code,
                "error": attempt.error,
                "trace_id": attempt.trace_id,
                "started_at": attempt.started_at,
                "finished_at": attempt.finished_at,
            }
            for attempt in detail["send_attempts"]
        ],
        "channel_receipts": [
            {
                "id": str(receipt.id),
                "channel_account_id": str(receipt.channel_account_id),
                "idempotency_key": receipt.idempotency_key,
                "external_send_id": receipt.external_send_id,
                "accepted_at": receipt.accepted_at,
            }
            for receipt in detail["channel_receipts"]
        ],
        "workflow": await _workflow_snapshot(request, ticket_id),
    }


async def _workflow_snapshot(request: Request, ticket_id: uuid.UUID) -> dict | None:
    checkpointer = getattr(request.app.state, "checkpointer", None)
    if checkpointer is None:
        return None
    try:
        return await review_workflow_snapshot(checkpointer, ticket_id)
    except Exception:
        return None


@router.post("/{ticket_id}/process")
async def process(
    ticket_id: uuid.UUID,
    request: Request,
    auth: AuthContext = Depends(require_csrf),
    db: AsyncSession = Depends(get_db),
):
    await get_ticket_for_employee(db, auth.employee, ticket_id, "read")
    await db.commit()
    return await process_ticket(
        request.app.state.sessionmaker,
        request.app.state.settings,
        ticket_id,
        auth.employee.id,
        adapters=request.app.state.agent_adapters,
        commerce=request.app.state.commerce_adapter,
        resolver=request.app.state.scope_resolver,
        checkpointer=request.app.state.checkpointer,
    )


@router.post("/{ticket_id}/diagnose")
async def diagnose(
    ticket_id: uuid.UUID,
    request: Request,
    auth: AuthContext = Depends(require_csrf),
):
    return await diagnose_ticket(
        request.app.state.sessionmaker,
        request.app.state.settings,
        request.app.state.commerce_adapter,
        request.app.state.scope_resolver,
        auth.employee.id,
        ticket_id,
    )


@router.post("/{ticket_id}/diagnose/apply")
async def diagnose_apply(
    ticket_id: uuid.UUID,
    payload: DiagnoseApplyPayload,
    auth: AuthContext = Depends(require_csrf),
    db: AsyncSession = Depends(get_db),
):
    return await apply_diagnosis(
        db,
        auth.employee,
        ticket_id,
        payload.diagnosis_id,
        payload.next_step,
        payload.next_owner_id,
        payload.next_follow_up_at,
        payload.note,
    )


@router.post("/{ticket_id}/drafts/{draft_id}/approve")
async def approve(
    ticket_id: uuid.UUID,
    draft_id: uuid.UUID,
    payload: ApprovePayload,
    request: Request,
    auth: AuthContext = Depends(require_csrf),
    db: AsyncSession = Depends(get_db),
):
    result = await approve_draft(
        db,
        request.app.state.settings,
        request.app.state.checkpointer,
        auth.employee,
        ticket_id,
        draft_id,
        payload.content_hash,
        payload.note,
    )
    return result


@router.post("/{ticket_id}/drafts/{draft_id}/reject")
async def reject(
    ticket_id: uuid.UUID,
    draft_id: uuid.UUID,
    payload: RejectPayload,
    auth: AuthContext = Depends(require_csrf),
    db: AsyncSession = Depends(get_db),
):
    return await reject_draft(db, auth.employee, ticket_id, draft_id, payload.note)


@router.post("/{ticket_id}/drafts")
async def edit(
    ticket_id: uuid.UUID,
    payload: EditDraftPayload,
    request: Request,
    auth: AuthContext = Depends(require_csrf),
    db: AsyncSession = Depends(get_db),
):
    return await edit_draft(
        db,
        request.app.state.checkpointer,
        auth.employee,
        ticket_id,
        payload.body,
        payload.note,
    )


@router.post("/{ticket_id}/assign")
async def assign(
    ticket_id: uuid.UUID,
    payload: AssignPayload,
    auth: AuthContext = Depends(require_csrf),
    db: AsyncSession = Depends(get_db),
):
    return await assign_ticket(
        db, auth.employee, ticket_id, uuid.UUID(payload.assignee_id)
    )


@router.post("/{ticket_id}/followups/{followup_id}")
async def followup_update(
    ticket_id: uuid.UUID,
    followup_id: uuid.UUID,
    payload: FollowupPayload,
    auth: AuthContext = Depends(require_csrf),
    db: AsyncSession = Depends(get_db),
):
    return await update_followup(
        db,
        auth.employee,
        ticket_id,
        followup_id,
        payload.conclusion,
        payload.basis,
        payload.notified,
    )


@router.post("/{ticket_id}/close")
async def close(
    ticket_id: uuid.UUID,
    payload: ClosePayload,
    auth: AuthContext = Depends(require_csrf),
    db: AsyncSession = Depends(get_db),
):
    return await close_ticket(
        db,
        auth.employee,
        ticket_id,
        payload.conclusion,
        payload.basis,
        payload.notified,
    )