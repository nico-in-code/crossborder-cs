from __future__ import annotations

import hmac
import uuid
from copy import deepcopy

from fastapi import HTTPException
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.channel import ChannelAdapter
from app.agents.workflow import (
    resume_review_workflow,
    review_workflow_snapshot,
    start_review_workflow,
)
from app.config import Settings
from app.models import (
    ChannelAccount,
    Conversation,
    Draft,
    Employee,
    Followup,
    FollowupStatus,
    Review,
    ReviewDecision,
    SendStatus,
    SendTask,
    Ticket,
    TicketEvent,
    TicketStatus,
)
from app.security import content_hash
from app.services.permission import get_ticket_for_employee
from app.trace import log_event, new_trace_id, utcnow


def _refund_requested(ticket: Ticket) -> bool:
    return any(intent.get("code") == "refund_request" for intent in ticket.intents or [])


async def _lock_ticket(session: AsyncSession, ticket_id: uuid.UUID) -> Ticket | None:
    return await session.scalar(
        select(Ticket).where(Ticket.id == ticket_id).with_for_update()
    )


async def _enabled_channel(session: AsyncSession, ticket: Ticket) -> ChannelAccount:
    account = await session.scalar(
        select(ChannelAccount)
        .where(
            ChannelAccount.tenant_id == ticket.tenant_id,
            ChannelAccount.store_id == ticket.store_id,
            ChannelAccount.enabled.is_(True),
            ChannelAccount.verified.is_(True),
        )
        .order_by(ChannelAccount.created_at)
        .limit(1)
    )
    if account is None:
        raise HTTPException(status_code=409, detail="no enabled channel for store")
    return account


async def _latest_draft(session: AsyncSession, ticket_id: uuid.UUID) -> Draft | None:
    return await session.scalar(
        select(Draft)
        .where(Draft.ticket_id == ticket_id)
        .order_by(Draft.version.desc())
        .limit(1)
    )


async def _invalidate_current_reviews(session: AsyncSession, ticket_id: uuid.UUID) -> None:
    await session.execute(
        update(Review)
        .where(
            Review.ticket_id == ticket_id,
            Review.is_current.is_(True),
            Review.decision == ReviewDecision.approved,
        )
        .values(is_current=False)
    )


async def _cancel_open_send_tasks(
    session: AsyncSession, ticket_id: uuid.UUID, reason: str
) -> None:
    await session.execute(
        update(SendTask)
        .where(
            SendTask.ticket_id == ticket_id,
            SendTask.status.in_([SendStatus.pending, SendStatus.claimed]),
        )
        .values(status=SendStatus.cancelled, last_error=reason)
    )


async def approve_draft(
    session: AsyncSession,
    settings: Settings,
    checkpointer,
    employee: Employee,
    ticket_id: uuid.UUID,
    draft_id: uuid.UUID,
    supplied_hash: str,
    note: str | None = None,
) -> dict:
    ticket = await get_ticket_for_employee(session, employee, ticket_id, "read")
    ticket = await _lock_ticket(session, ticket_id)
    if ticket is None:
        raise HTTPException(status_code=404, detail="ticket not found")
    if ticket.status is TicketStatus.closed:
        raise HTTPException(status_code=409, detail="ticket already closed")
    if _refund_requested(ticket) and not employee.refund_authority:
        raise HTTPException(status_code=403, detail="refund authority required")

    draft = await session.get(Draft, draft_id)
    if draft is None or draft.ticket_id != ticket.id:
        raise HTTPException(status_code=404, detail="draft not found")
    latest = await _latest_draft(session, ticket.id)
    if latest is None or latest.id != draft.id:
        raise HTTPException(status_code=409, detail="draft is not the latest version")
    if not hmac.compare_digest(draft.content_hash, supplied_hash):
        raise HTTPException(status_code=409, detail="draft content hash mismatch")

    existing = await session.scalar(
        select(Review)
        .where(
            Review.ticket_id == ticket.id,
            Review.draft_id == draft.id,
            Review.draft_content_hash == draft.content_hash,
            Review.decision == ReviewDecision.approved,
            Review.is_current.is_(True),
        )
        .order_by(Review.created_at.desc())
        .limit(1)
    )
    if existing is not None:
        task = await session.scalar(
            select(SendTask).where(SendTask.review_id == existing.id)
        )
        if task is None:
            task = await _create_send_task(session, ticket, draft, existing)
            await session.commit()
        return {
            "idempotent": True,
            "review_id": str(existing.id),
            "send_task_id": str(task.id),
            "send_status": task.status.value,
            "draft_version": draft.version,
        }

    await _invalidate_current_reviews(session, ticket.id)
    await _cancel_open_send_tasks(session, ticket.id, "superseded_by_new_review")

    review = Review(
        tenant_id=ticket.tenant_id,
        ticket_id=ticket.id,
        draft_id=draft.id,
        draft_version=draft.version,
        draft_content_hash=draft.content_hash,
        reviewer_id=employee.id,
        decision=ReviewDecision.approved,
        note=note,
        is_current=True,
        decided_at=utcnow(),
    )
    session.add(review)
    await session.flush()
    task = await _create_send_task(session, ticket, draft, review)
    ticket.status = TicketStatus.awaiting_review
    ticket.next_step = "awaiting_send"
    ticket.updated_at = utcnow()
    session.add(
        TicketEvent(
            tenant_id=ticket.tenant_id,
            ticket_id=ticket.id,
            kind="review_approved",
            actor_id=employee.id,
            payload={
                "review_id": str(review.id),
                "draft_id": str(draft.id),
                "draft_version": draft.version,
                "content_hash": draft.content_hash,
                "send_task_id": str(task.id),
            },
        )
    )
    await session.commit()

    workflow = None
    if checkpointer is not None:
        try:
            workflow = await resume_review_workflow(
                checkpointer,
                ticket.id,
                {
                    "decision": "approved",
                    "review_id": str(review.id),
                    "draft_id": str(draft.id),
                    "content_hash": draft.content_hash,
                },
            )
        except Exception as exc:  # noqa: BLE001 - approval is already persisted
            log_event(
                "workflow_resume_failed",
                ticket_id=str(ticket.id),
                error=type(exc).__name__,
            )
            workflow = {"status": "resume_failed", "error": type(exc).__name__}

    return {
        "idempotent": False,
        "review_id": str(review.id),
        "send_task_id": str(task.id),
        "send_status": task.status.value,
        "draft_version": draft.version,
        "workflow": workflow,
    }


async def _create_send_task(
    session: AsyncSession, ticket: Ticket, draft: Draft, review: Review
) -> SendTask:
    existing = await session.scalar(
        select(SendTask).where(SendTask.review_id == review.id)
    )
    if existing is not None:
        return existing
    account = await _enabled_channel(session, ticket)
    conversation = await session.get(Conversation, ticket.conversation_id)
    idempotency_key = f"{ticket.id}:{draft.id}:{draft.content_hash[:24]}"
    task = SendTask(
        tenant_id=ticket.tenant_id,
        ticket_id=ticket.id,
        review_id=review.id,
        channel_account_id=account.id,
        idempotency_key=idempotency_key,
        status=SendStatus.pending,
        payload={
            "ticket_id": str(ticket.id),
            "ticket_number": ticket.number,
            "draft_id": str(draft.id),
            "draft_version": draft.version,
            "content_hash": draft.content_hash,
            "language": "ja",
            "body": draft.body,
            "review_id": str(review.id),
            "channel_account_code": account.code,
            "buyer_external_id": (
                conversation.buyer_external_id if conversation else None
            ),
        },
        next_retry_at=utcnow(),
    )
    session.add(task)
    await session.flush()
    return task


async def edit_draft(
    session: AsyncSession,
    checkpointer,
    employee: Employee,
    ticket_id: uuid.UUID,
    body: str,
    note: str | None = None,
) -> dict:
    ticket = await get_ticket_for_employee(session, employee, ticket_id, "read")
    ticket = await _lock_ticket(session, ticket_id)
    if ticket is None:
        raise HTTPException(status_code=404, detail="ticket not found")
    if ticket.status is TicketStatus.closed:
        raise HTTPException(status_code=409, detail="ticket already closed")
    if _refund_requested(ticket) and not employee.refund_authority:
        raise HTTPException(status_code=403, detail="refund authority required")
    if not body.strip():
        raise HTTPException(status_code=422, detail="draft body is required")

    latest = await _latest_draft(session, ticket.id)
    if latest is None:
        raise HTTPException(status_code=409, detail="ticket has no draft")

    facts = deepcopy(latest.structured_facts or {})
    facts["edited"] = True
    facts["edit_note"] = note
    draft = Draft(
        tenant_id=ticket.tenant_id,
        ticket_id=ticket.id,
        version=latest.version + 1,
        language="ja",
        body=body.strip(),
        content_hash=content_hash(body.strip()),
        structured_facts=facts,
        evidence=latest.evidence or [],
        status="ready",
        created_by_id=employee.id,
    )
    session.add(draft)
    await session.flush()

    await _invalidate_current_reviews(session, ticket.id)
    await _cancel_open_send_tasks(session, ticket.id, "draft_edited_after_approval")

    ticket.status = TicketStatus.awaiting_review
    ticket.next_step = "awaiting_review"
    ticket.updated_at = utcnow()
    session.add(
        TicketEvent(
            tenant_id=ticket.tenant_id,
            ticket_id=ticket.id,
            kind="draft_edited",
            actor_id=employee.id,
            payload={
                "draft_id": str(draft.id),
                "draft_version": draft.version,
                "previous_version": latest.version,
                "content_hash": draft.content_hash,
            },
        )
    )
    await session.commit()

    if checkpointer is not None:
        try:
            snapshot = await review_workflow_snapshot(checkpointer, ticket.id)
            if not snapshot["next"]:
                await start_review_workflow(checkpointer, ticket.id, draft.id)
        except Exception as exc:  # noqa: BLE001 - checkpoint is a gate, not the source of truth
            log_event(
                "workflow_restart_failed",
                ticket_id=str(ticket.id),
                error=type(exc).__name__,
            )

    return {
        "draft_id": str(draft.id),
        "version": draft.version,
        "content_hash": draft.content_hash,
        "body": draft.body,
    }


async def reject_draft(
    session: AsyncSession,
    employee: Employee,
    ticket_id: uuid.UUID,
    draft_id: uuid.UUID,
    note: str | None = None,
) -> dict:
    ticket = await get_ticket_for_employee(session, employee, ticket_id, "read")
    ticket = await _lock_ticket(session, ticket_id)
    if ticket is None:
        raise HTTPException(status_code=404, detail="ticket not found")
    if _refund_requested(ticket) and not employee.refund_authority:
        raise HTTPException(status_code=403, detail="refund authority required")
    draft = await session.get(Draft, draft_id)
    if draft is None or draft.ticket_id != ticket.id:
        raise HTTPException(status_code=404, detail="draft not found")
    await _invalidate_current_reviews(session, ticket.id)
    await _cancel_open_send_tasks(session, ticket.id, "draft_rejected")
    review = Review(
        tenant_id=ticket.tenant_id,
        ticket_id=ticket.id,
        draft_id=draft.id,
        draft_version=draft.version,
        draft_content_hash=draft.content_hash,
        reviewer_id=employee.id,
        decision=ReviewDecision.rejected,
        note=note,
        is_current=True,
        decided_at=utcnow(),
    )
    session.add(review)
    ticket.next_step = "draft_rejected"
    ticket.updated_at = utcnow()
    session.add(
        TicketEvent(
            tenant_id=ticket.tenant_id,
            ticket_id=ticket.id,
            kind="review_rejected",
            actor_id=employee.id,
            payload={"review_id": str(review.id), "draft_id": str(draft.id)},
        )
    )
    await session.commit()
    return {"review_id": str(review.id), "decision": "rejected"}


async def assign_ticket(
    session: AsyncSession, employee: Employee, ticket_id: uuid.UUID, assignee_id: uuid.UUID
) -> dict:
    ticket = await get_ticket_for_employee(session, employee, ticket_id, "assign")
    assignee = await session.get(Employee, assignee_id)
    if (
        assignee is None
        or assignee.tenant_id != ticket.tenant_id
        or not assignee.active
    ):
        raise HTTPException(status_code=404, detail="assignee not found")
    ticket.assignee_id = assignee.id
    ticket.updated_at = utcnow()
    session.add(
        TicketEvent(
            tenant_id=ticket.tenant_id,
            ticket_id=ticket.id,
            kind="assigned",
            actor_id=employee.id,
            payload={"assignee_id": str(assignee.id)},
        )
    )
    await session.commit()
    return {"ticket_id": str(ticket.id), "assignee_id": str(assignee.id)}


async def ensure_followup(
    session: AsyncSession, ticket: Ticket, owner_id: uuid.UUID | None
) -> Followup:
    refund = _refund_requested(ticket)
    kind = "refund" if refund else "reply"
    existing = await session.scalar(
        select(Followup).where(
            Followup.ticket_id == ticket.id,
            Followup.kind == kind,
            Followup.status.in_(
                [FollowupStatus.pending, FollowupStatus.in_progress]
            ),
        )
    )
    if existing is not None:
        return existing
    followup = Followup(
        tenant_id=ticket.tenant_id,
        ticket_id=ticket.id,
        kind=kind,
        status=FollowupStatus.pending,
        owner_id=owner_id or ticket.assignee_id,
        due_at=utcnow(),
        resolved_at=None,
    )
    session.add(followup)
    await session.flush()
    return followup


async def update_followup(
    session: AsyncSession,
    employee: Employee,
    ticket_id: uuid.UUID,
    followup_id: uuid.UUID,
    conclusion: str,
    basis: str,
    notified: bool,
) -> dict:
    ticket = await get_ticket_for_employee(session, employee, ticket_id, "close")
    followup = await session.get(Followup, followup_id)
    if followup is None or followup.ticket_id != ticket.id:
        raise HTTPException(status_code=404, detail="followup not found")
    if not conclusion.strip() or not basis.strip():
        raise HTTPException(status_code=422, detail="conclusion and basis are required")
    followup.conclusion = conclusion.strip()
    followup.basis = basis.strip()
    if notified:
        followup.notified_at = utcnow()
        followup.status = FollowupStatus.notified
        followup.resolved_at = utcnow()
    else:
        followup.status = FollowupStatus.in_progress
    session.add(
        TicketEvent(
            tenant_id=ticket.tenant_id,
            ticket_id=ticket.id,
            kind="followup_updated",
            actor_id=employee.id,
            payload={
                "followup_id": str(followup.id),
                "notified": notified,
                "status": followup.status.value,
            },
        )
    )
    await session.commit()
    return {
        "followup_id": str(followup.id),
        "status": followup.status.value,
        "notified_at": followup.notified_at.isoformat() if followup.notified_at else None,
    }


async def close_ticket(
    session: AsyncSession,
    employee: Employee,
    ticket_id: uuid.UUID,
    conclusion: str,
    basis: str,
    notified: bool,
) -> dict:
    ticket = await get_ticket_for_employee(session, employee, ticket_id, "close")
    ticket = await _lock_ticket(session, ticket_id)
    if ticket is None:
        raise HTTPException(status_code=404, detail="ticket not found")
    if ticket.status is TicketStatus.closed:
        return {"ticket_id": str(ticket.id), "status": "closed", "idempotent": True}
    if not conclusion.strip() or not basis.strip():
        raise HTTPException(status_code=422, detail="conclusion and basis are required")
    if not notified:
        raise HTTPException(status_code=409, detail="buyer must be notified before closing")

    active_sends = await session.scalar(
        select(func.count()).select_from(SendTask).where(
            SendTask.ticket_id == ticket.id,
            SendTask.status.in_(
                [
                    SendStatus.pending,
                    SendStatus.claimed,
                    SendStatus.submitted,
                    SendStatus.unknown,
                ]
            ),
        )
    )
    if active_sends:
        raise HTTPException(
            status_code=409, detail="ticket has unresolved send tasks"
        )

    followup = await session.scalar(
        select(Followup)
        .where(Followup.ticket_id == ticket.id)
        .order_by(Followup.created_at.desc())
        .limit(1)
    )
    if followup is None:
        raise HTTPException(status_code=409, detail="ticket has no followup")
    followup.conclusion = conclusion.strip()
    followup.basis = basis.strip()
    followup.notified_at = utcnow()
    followup.status = FollowupStatus.notified
    followup.resolved_at = utcnow()

    ticket.status = TicketStatus.closed
    ticket.closed_at = utcnow()
    ticket.next_step = "closed"
    ticket.updated_at = utcnow()
    session.add(
        TicketEvent(
            tenant_id=ticket.tenant_id,
            ticket_id=ticket.id,
            kind="ticket_closed",
            actor_id=employee.id,
            trace_id=new_trace_id(),
            payload={
                "followup_id": str(followup.id),
                "conclusion": conclusion.strip(),
                "basis": basis.strip(),
            },
        )
    )
    await session.commit()
    return {
        "ticket_id": str(ticket.id),
        "status": "closed",
        "followup_id": str(followup.id),
        "idempotent": False,
    }