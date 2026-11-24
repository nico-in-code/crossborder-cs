from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    AgentRun,
    Attachment,
    ChannelReceipt,
    Conversation,
    Draft,
    Employee,
    EmployeeRole,
    Followup,
    Message,
    Review,
    SendAttempt,
    SendTask,
    Store,
    StoreGrant,
    Ticket,
    TicketEvent,
    ToolCall,
)
from app.services.permission import accessible_store_ids, get_ticket_for_employee


async def list_tickets(
    session: AsyncSession,
    employee: Employee,
    status: str | None = None,
    limit: int = 100,
) -> list[tuple[Ticket, Store]]:
    store_ids = await accessible_store_ids(session, employee)
    if not store_ids:
        return []
    query = (
        select(Ticket, Store)
        .join(Store, Ticket.store_id == Store.id)
        .where(
            Ticket.tenant_id == employee.tenant_id,
            Ticket.store_id.in_(store_ids),
        )
        .order_by(Ticket.updated_at.desc())
        .limit(limit)
    )
    if status:
        query = query.where(Ticket.status == status)
    return list((await session.execute(query)).all())


async def get_ticket_detail(
    session: AsyncSession,
    employee: Employee,
    ticket_id: uuid.UUID,
) -> dict:
    ticket = await get_ticket_for_employee(session, employee, ticket_id, "read")
    store = await session.get(Store, ticket.store_id)
    conversation = await session.get(Conversation, ticket.conversation_id)
    assignee = await session.get(Employee, ticket.assignee_id) if ticket.assignee_id else None
    next_owner = (
        await session.get(Employee, ticket.next_owner_id)
        if ticket.next_owner_id
        else None
    )
    owners = list(
        (
            await session.scalars(
                select(Employee)
                .join(StoreGrant, StoreGrant.employee_id == Employee.id)
                .where(
                    Employee.tenant_id == ticket.tenant_id,
                    Employee.active.is_(True),
                    Employee.role.in_([EmployeeRole.agent, EmployeeRole.supervisor]),
                    StoreGrant.tenant_id == ticket.tenant_id,
                    StoreGrant.store_id == ticket.store_id,
                )
                .order_by(Employee.display_name)
            )
        ).all()
    )
    messages = list(
        (
            await session.scalars(
                select(Message)
                .where(Message.ticket_id == ticket.id)
                .order_by(Message.created_at)
            )
        ).all()
    )
    attachments = list(
        (
            await session.scalars(
                select(Attachment)
                .where(Attachment.ticket_id == ticket.id)
                .order_by(Attachment.created_at.desc())
            )
        ).all()
    )
    events = list(
        (
            await session.scalars(
                select(TicketEvent)
                .where(TicketEvent.ticket_id == ticket.id)
                .order_by(TicketEvent.created_at)
            )
        ).all()
    )
    followups = list(
        (
            await session.scalars(
                select(Followup)
                .where(Followup.ticket_id == ticket.id)
                .order_by(Followup.created_at)
            )
        ).all()
    )
    drafts = list(
        (
            await session.scalars(
                select(Draft)
                .where(Draft.ticket_id == ticket.id)
                .order_by(Draft.version.desc())
            )
        ).all()
    )
    agent_runs = list(
        (
            await session.scalars(
                select(AgentRun)
                .where(AgentRun.ticket_id == ticket.id)
                .order_by(AgentRun.created_at.desc())
            )
        ).all()
    )
    tool_calls = list(
        (
            await session.scalars(
                select(ToolCall)
                .join(AgentRun, ToolCall.agent_run_id == AgentRun.id)
                .where(AgentRun.ticket_id == ticket.id)
                .order_by(ToolCall.created_at, ToolCall.step_no)
            )
        ).all()
    )
    reviews = list(
        (
            await session.scalars(
                select(Review)
                .where(Review.ticket_id == ticket.id)
                .order_by(Review.created_at.desc())
            )
        ).all()
    )
    send_tasks = list(
        (
            await session.scalars(
                select(SendTask)
                .where(SendTask.ticket_id == ticket.id)
                .order_by(SendTask.created_at.desc())
            )
        ).all()
    )
    task_ids = [task.id for task in send_tasks]
    send_attempts = (
        list(
            (
                await session.scalars(
                    select(SendAttempt)
                    .where(SendAttempt.send_task_id.in_(task_ids))
                    .order_by(SendAttempt.created_at)
                )
            ).all()
        )
        if task_ids
        else []
    )
    receipts = (
        list(
            (
                await session.scalars(
                    select(ChannelReceipt).where(
                        ChannelReceipt.idempotency_key.in_(
                            [task.idempotency_key for task in send_tasks]
                        )
                    )
                )
            ).all()
        )
        if send_tasks
        else []
    )
    latest_diagnosis_run = next(
        (run for run in agent_runs if run.graph == "diagnose"), None
    )
    latest_diagnosis = (
        {
            "diagnosis_id": str(latest_diagnosis_run.id),
            "trace_id": latest_diagnosis_run.trace_id,
            "status": latest_diagnosis_run.status.value,
            "created_at": latest_diagnosis_run.created_at,
            **(latest_diagnosis_run.state_summary or {}),
        }
        if latest_diagnosis_run is not None
        else None
    )
    return {
        "ticket": ticket,
        "store": store,
        "conversation": conversation,
        "assignee": assignee,
        "next_owner": next_owner,
        "owners": owners,
        "messages": messages,
        "attachments": attachments,
        "events": events,
        "followups": followups,
        "drafts": drafts,
        "latest_draft": drafts[0] if drafts else None,
        "agent_runs": agent_runs,
        "tool_calls": tool_calls,
        "reviews": reviews,
        "send_tasks": send_tasks,
        "send_attempts": send_attempts,
        "channel_receipts": receipts,
        "latest_diagnosis": latest_diagnosis,
    }