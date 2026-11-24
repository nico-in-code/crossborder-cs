from __future__ import annotations

import uuid
from datetime import timedelta

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.adapters.channel import ChannelAdapter, ChannelRejected, ChannelTimeout
from app.config import Settings
from app.models import (
    Draft,
    Review,
    ReviewDecision,
    SendAttempt,
    SendOutcome,
    SendStatus,
    SendTask,
    Ticket,
    TicketEvent,
    TicketStatus,
)
from app.services.review import ensure_followup
from app.trace import log_event, new_trace_id, utcnow


async def claim_next_task(
    sessionmaker: async_sessionmaker[AsyncSession], lease_seconds: int = 30
) -> tuple[uuid.UUID, int] | None:
    now = utcnow()
    async with sessionmaker() as session:
        task = await session.scalar(
            select(SendTask)
            .where(
                or_(
                    and_(
                        SendTask.status == SendStatus.pending,
                        SendTask.next_retry_at <= now,
                    ),
                    and_(
                        SendTask.status == SendStatus.claimed,
                        SendTask.lease_until.is_not(None),
                        SendTask.lease_until < now,
                    ),
                )
            )
            .order_by(SendTask.next_retry_at, SendTask.created_at)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if task is None:
            await session.commit()
            return None
        task.status = SendStatus.claimed
        task.lease_until = now + timedelta(seconds=lease_seconds)
        task.attempt_count += 1
        attempt_no = task.attempt_count
        task_id = task.id
        await session.commit()
        return task_id, attempt_no


async def _revalidation_error(
    session: AsyncSession, task: SendTask
) -> str | None:
    ticket = await session.get(Ticket, task.ticket_id)
    if ticket is None:
        return "ticket_missing"
    if ticket.status is TicketStatus.closed:
        return "ticket_closed"
    review = await session.get(Review, task.review_id)
    if review is None:
        return "review_missing"
    if review.decision is not ReviewDecision.approved or not review.is_current:
        return "review_not_current"
    draft = await session.get(Draft, review.draft_id)
    if draft is None:
        return "draft_missing"
    latest_version = await session.scalar(
        select(Draft.version)
        .where(Draft.ticket_id == ticket.id)
        .order_by(Draft.version.desc())
        .limit(1)
    )
    if latest_version != draft.version:
        return "draft_superseded"
    payload = task.payload or {}
    if not (
        review.draft_version == draft.version
        and review.draft_content_hash == draft.content_hash
        and payload.get("content_hash") == draft.content_hash
        and payload.get("review_id") == str(review.id)
        and payload.get("draft_id") == str(draft.id)
    ):
        return "hash_mismatch"
    return None


async def process_next_send(
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    channel: ChannelAdapter,
) -> dict | None:
    claimed = await claim_next_task(sessionmaker)
    if claimed is None:
        return None
    task_id, attempt_no = claimed
    trace_id = new_trace_id()
    now = utcnow()

    async with sessionmaker() as session:
        task = await session.get(SendTask, task_id)
        if task is None:
            return None
        stale_reason = await _revalidation_error(session, task)
        if stale_reason is not None:
            task.status = SendStatus.cancelled
            task.last_error = stale_reason
            task.lease_until = None
            session.add(
                TicketEvent(
                    tenant_id=task.tenant_id,
                    ticket_id=task.ticket_id,
                    kind="send_cancelled_stale",
                    trace_id=trace_id,
                    payload={
                        "send_task_id": str(task.id),
                        "reason": stale_reason,
                        "idempotency_key": task.idempotency_key,
                    },
                )
            )
            await session.commit()
            return {"send_task_id": str(task.id), "status": "cancelled", "reason": stale_reason}

        review = await session.get(Review, task.review_id)
        attempt = SendAttempt(
            tenant_id=task.tenant_id,
            send_task_id=task.id,
            attempt_no=attempt_no,
            outcome=SendOutcome.unknown,
            trace_id=trace_id,
            started_at=now,
        )
        session.add(attempt)
        await session.flush()

        receipt = await channel.lookup_receipt(
            task.channel_account_id, task.idempotency_key
        )
        if receipt is not None:
            attempt.outcome = SendOutcome.channel_accepted
            attempt.external_send_id = receipt.external_send_id
            attempt.response_code = "recovered_receipt"
            attempt.finished_at = utcnow()
            task.status = SendStatus.acked
            task.lease_until = None
            await _mark_ticket_open_and_followup(session, task, review)
            session.add(
                TicketEvent(
                    tenant_id=task.tenant_id,
                    ticket_id=task.ticket_id,
                    kind="send_recovered_from_receipt",
                    trace_id=trace_id,
                    payload={
                        "send_task_id": str(task.id),
                        "external_send_id": receipt.external_send_id,
                        "idempotency_key": task.idempotency_key,
                    },
                )
            )
            await session.commit()
            return {"send_task_id": str(task.id), "status": "acked", "recovered": True}

        payload = task.payload or {}
        try:
            receipt = await channel.send(
                tenant_id=task.tenant_id,
                channel_account_id=task.channel_account_id,
                idempotency_key=task.idempotency_key,
                body_hash=payload.get("content_hash", ""),
                payload={
                    "ticket_id": payload.get("ticket_id"),
                    "ticket_number": payload.get("ticket_number"),
                    "draft_id": payload.get("draft_id"),
                    "draft_version": payload.get("draft_version"),
                    "body": payload.get("body"),
                    "buyer_external_id": payload.get("buyer_external_id"),
                },
            )
            attempt.outcome = SendOutcome.channel_accepted
            attempt.external_send_id = receipt.external_send_id
            attempt.response_code = "200"
            attempt.finished_at = utcnow()
            task.status = SendStatus.acked
            task.lease_until = None
            await _mark_ticket_open_and_followup(session, task, review)
            session.add(
                TicketEvent(
                    tenant_id=task.tenant_id,
                    ticket_id=task.ticket_id,
                    kind="send_acked",
                    trace_id=trace_id,
                    payload={
                        "send_task_id": str(task.id),
                        "external_send_id": receipt.external_send_id,
                        "idempotency_key": task.idempotency_key,
                        "attempt_no": attempt_no,
                    },
                )
            )
            await session.commit()
            return {
                "send_task_id": str(task.id),
                "status": "acked",
                "external_send_id": receipt.external_send_id,
            }
        except ChannelTimeout as exc:
            attempt.outcome = SendOutcome.unknown
            attempt.error = f"timeout:{exc}"
            attempt.finished_at = utcnow()
            task.status = SendStatus.unknown
            task.last_error = "channel_timeout_after_accept"
            task.lease_until = None
            task.next_retry_at = utcnow() + timedelta(seconds=5)
            session.add(
                TicketEvent(
                    tenant_id=task.tenant_id,
                    ticket_id=task.ticket_id,
                    kind="send_unknown",
                    trace_id=trace_id,
                    payload={
                        "send_task_id": str(task.id),
                        "idempotency_key": task.idempotency_key,
                        "attempt_no": attempt_no,
                    },
                )
            )
            await session.commit()
            log_event(
                "send_unknown",
                ticket_id=str(task.ticket_id),
                send_task_id=str(task.id),
                trace_id=trace_id,
            )
            return {"send_task_id": str(task.id), "status": "unknown"}
        except ChannelRejected as exc:
            attempt.outcome = SendOutcome.failed
            attempt.error = f"rejected:{exc}"
            attempt.response_code = "rejected"
            attempt.finished_at = utcnow()
            task.status = SendStatus.failed
            task.last_error = "channel_rejected"
            task.lease_until = None
            task.next_retry_at = utcnow() + timedelta(seconds=60)
            session.add(
                TicketEvent(
                    tenant_id=task.tenant_id,
                    ticket_id=task.ticket_id,
                    kind="send_failed",
                    trace_id=trace_id,
                    payload={
                        "send_task_id": str(task.id),
                        "attempt_no": attempt_no,
                        "error": "channel_rejected",
                    },
                )
            )
            await session.commit()
            return {"send_task_id": str(task.id), "status": "failed"}
        except Exception as exc:  # noqa: BLE001 - unknown send state must be reconciled
            attempt.outcome = SendOutcome.unknown
            attempt.error = type(exc).__name__
            attempt.finished_at = utcnow()
            task.status = SendStatus.unknown
            task.last_error = f"unexpected:{type(exc).__name__}"
            task.lease_until = None
            task.next_retry_at = utcnow() + timedelta(seconds=5)
            session.add(
                TicketEvent(
                    tenant_id=task.tenant_id,
                    ticket_id=task.ticket_id,
                    kind="send_unknown",
                    trace_id=trace_id,
                    payload={
                        "send_task_id": str(task.id),
                        "attempt_no": attempt_no,
                        "error": type(exc).__name__,
                    },
                )
            )
            await session.commit()
            return {"send_task_id": str(task.id), "status": "unknown"}


async def _mark_ticket_open_and_followup(
    session: AsyncSession, task: SendTask, review: Review | None
) -> None:
    ticket = await session.get(Ticket, task.ticket_id)
    if ticket is None or ticket.status is TicketStatus.closed:
        return
    ticket.status = TicketStatus.open
    ticket.next_step = "followup"
    ticket.updated_at = utcnow()
    owner_id = review.reviewer_id if review is not None else None
    followup = await ensure_followup(session, ticket, owner_id)
    ticket.next_owner_id = followup.owner_id
    ticket.next_follow_up_at = followup.due_at


async def reconcile_pending(
    sessionmaker: async_sessionmaker[AsyncSession],
    channel: ChannelAdapter,
    limit: int = 20,
) -> list[dict]:
    async with sessionmaker() as session:
        task_ids = list(
            await session.scalars(
                select(SendTask.id)
                .where(
                    SendTask.status.in_([SendStatus.unknown, SendStatus.submitted])
                )
                .order_by(SendTask.updated_at)
                .limit(limit)
            )
        )

    results: list[dict] = []
    for task_id in task_ids:
        trace_id = new_trace_id()
        async with sessionmaker() as session:
            task = await session.get(SendTask, task_id)
            if task is None or task.status not in {
                SendStatus.unknown,
                SendStatus.submitted,
            }:
                continue
            receipt = await channel.lookup_receipt(
                task.channel_account_id, task.idempotency_key
            )
            if receipt is not None:
                task.status = SendStatus.reconciled_sent
                task.last_error = None
                task.lease_until = None
                attempt = await session.scalar(
                    select(SendAttempt)
                    .where(
                        SendAttempt.send_task_id == task.id,
                        SendAttempt.outcome == SendOutcome.unknown,
                    )
                    .order_by(SendAttempt.attempt_no.desc())
                    .limit(1)
                )
                if attempt is not None:
                    attempt.outcome = SendOutcome.channel_accepted
                    attempt.external_send_id = receipt.external_send_id
                    attempt.response_code = "reconciled"
                    attempt.finished_at = utcnow()
                review = await session.get(Review, task.review_id)
                await _mark_ticket_open_and_followup(session, task, review)
                session.add(
                    TicketEvent(
                        tenant_id=task.tenant_id,
                        ticket_id=task.ticket_id,
                        kind="send_reconciled_sent",
                        trace_id=trace_id,
                        payload={
                            "send_task_id": str(task.id),
                            "external_send_id": receipt.external_send_id,
                            "idempotency_key": task.idempotency_key,
                        },
                    )
                )
                await session.commit()
                results.append(
                    {
                        "send_task_id": str(task.id),
                        "status": "reconciled_sent",
                        "external_send_id": receipt.external_send_id,
                    }
                )
            else:
                task.status = SendStatus.manual
                task.last_error = "receipt_not_found_manual_review"
                task.lease_until = None
                session.add(
                    TicketEvent(
                        tenant_id=task.tenant_id,
                        ticket_id=task.ticket_id,
                        kind="send_reconcile_manual",
                        trace_id=trace_id,
                        payload={
                            "send_task_id": str(task.id),
                            "idempotency_key": task.idempotency_key,
                        },
                    )
                )
                await session.commit()
                results.append(
                    {"send_task_id": str(task.id), "status": "manual"}
                )
    return results


async def process_ready_tasks(
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    channel: ChannelAdapter,
    max_tasks: int = 10,
) -> list[dict]:
    results: list[dict] = []
    for _ in range(max_tasks):
        result = await process_next_send(sessionmaker, settings, channel)
        if result is None:
            break
        results.append(result)
    results.extend(await reconcile_pending(sessionmaker, channel))
    return results