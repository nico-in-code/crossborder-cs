from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.adapters.commerce import CommerceAdapter, ScopeResolver
from app.adapters.embed import HashEmbeddingAdapter
from app.adapters.llm import DeterministicLLMAdapter
from app.adapters.rerank import LexicalReranker
from app.adapters.translate import DeterministicTranslator
from app.agents.business import AgentRuntime
from app.agents.master import AgentAdapters, build_master_graph
from app.agents.workflow import start_review_workflow
from app.config import Settings
from app.mcp.context import BUSINESS_TOOL_NAMES, RunContext, run_context_token
from app.models import (
    AgentRun,
    Conversation,
    Employee,
    Message,
    MessageDirection,
    RunStatus,
    Ticket,
    TicketEvent,
    TicketStatus,
    ToolCall,
    ToolCallStatus,
    TranslationState,
)
from app.services.draft import create_draft
from app.services.permission import get_ticket_for_employee
from app.trace import log_event, new_trace_id, utcnow


def default_adapters() -> AgentAdapters:
    return AgentAdapters(
        llm=DeterministicLLMAdapter(),
        translator=DeterministicTranslator(),
        embedder=HashEmbeddingAdapter(),
        reranker=LexicalReranker(),
    )


async def process_ticket(
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    ticket_id: uuid.UUID,
    actor_employee_id: uuid.UUID,
    *,
    adapters: AgentAdapters | None = None,
    commerce: CommerceAdapter,
    resolver: ScopeResolver,
    checkpointer=None,
) -> dict:
    adapters = adapters or default_adapters()
    trace_id = new_trace_id()

    async with sessionmaker() as session:
        employee = await session.get(Employee, actor_employee_id)
        if employee is None:
            raise LookupError("actor employee not found")
        ticket = await get_ticket_for_employee(session, employee, ticket_id, "read")
        conversation = await session.get(Conversation, ticket.conversation_id)
        if conversation is None:
            raise LookupError("conversation not found")
        message = await session.scalar(
            select(Message)
            .where(
                Message.ticket_id == ticket.id,
                Message.direction == MessageDirection.inbound,
            )
            .order_by(Message.created_at.desc())
            .limit(1)
        )
        if message is None:
            raise LookupError("ticket has no inbound message")

        run = AgentRun(
            tenant_id=ticket.tenant_id,
            ticket_id=ticket.id,
            thread_id=str(ticket.id),
            graph="master",
            status=RunStatus.running,
            state_summary={},
            trace_id=trace_id,
        )
        session.add(run)
        await session.flush()

        context = RunContext(
            tenant_id=ticket.tenant_id,
            store_ids={ticket.store_id},
            employee_id=str(employee.id),
            role=employee.role.value,
            allowed_tools=BUSINESS_TOOL_NAMES,
            ticket_id=str(ticket.id),
            agent_run_id=str(run.id),
            graph="master",
        )
        runtime = AgentRuntime(
            settings=settings,
            sessionmaker=sessionmaker,
            commerce=commerce,
            resolver=resolver,
            context_token=run_context_token(settings, context),
            tenant_id=ticket.tenant_id,
            store_id=ticket.store_id,
            ticket_id=str(ticket.id),
            buyer_external_id=conversation.buyer_external_id,
        )
        graph = build_master_graph(adapters, runtime, sessionmaker)
        initial_state = {
            "ticket_id": str(ticket.id),
            "store_id": str(ticket.store_id),
            "original_text": message.body_text,
            "language": message.original_language,
            "order_candidates": list(ticket.order_candidates or []),
            "intents": [],
            "entities": {},
            "risk_flags": [],
            "requires_human_review": False,
            "route": "human",
            "faq": None,
            "evidence": [],
            "gaps": [],
            "business": {},
            "tool_outcomes": [],
        }

        try:
            state = await graph.ainvoke(
                initial_state, config={"recursion_limit": 20}
            )
        except Exception as exc:
            run.status = RunStatus.failed
            run.state_summary = {"error": type(exc).__name__}
            session.add(
                TicketEvent(
                    tenant_id=ticket.tenant_id,
                    ticket_id=ticket.id,
                    kind="agent_failed",
                    trace_id=trace_id,
                    payload={"error": type(exc).__name__},
                )
            )
            await session.commit()
            raise

        message.translated_text = state.get("translated_text") or message.body_text
        message.translated_language = "zh"
        message.translation_state = TranslationState.done

        for outcome in state.get("tool_outcomes", []):
            session.add(
                ToolCall(
                    tenant_id=ticket.tenant_id,
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

        entities = state.get("entities") or {}
        order_candidates = list(entities.get("order_nos") or ticket.order_candidates or [])
        ticket.intents = state.get("intents", [])
        ticket.risk_flags = state.get("risk_flags", [])
        ticket.order_candidates = order_candidates
        ticket.summary = state.get("translated_text")
        ticket.next_step = "awaiting_review"
        ticket.status = TicketStatus.awaiting_review
        ticket.updated_at = utcnow()

        draft = await create_draft(session, ticket, state)

        session.add(
            TicketEvent(
                tenant_id=ticket.tenant_id,
                ticket_id=ticket.id,
                kind="agent_processed",
                trace_id=trace_id,
                payload={
                    "route": state.get("route"),
                    "intents": [intent["code"] for intent in state.get("intents", [])],
                    "risk_flags": state.get("risk_flags", []),
                    "draft_id": str(draft.id),
                    "draft_version": draft.version,
                    "tool_calls": len(state.get("tool_outcomes", [])),
                    "gaps": state.get("gaps", []),
                },
            )
        )

        run.status = RunStatus.awaiting_review
        run.state_summary = {
            "route": state.get("route"),
            "intents": [intent["code"] for intent in state.get("intents", [])],
            "risk_flags": state.get("risk_flags", []),
            "gaps": state.get("gaps", []),
            "evidence_count": len(state.get("evidence", [])),
            "tool_calls": [
                {
                    "tool_name": outcome["tool_name"],
                    "status": outcome["status"],
                    "step_no": outcome["step_no"],
                    "tool_call_id": outcome["tool_call_id"],
                }
                for outcome in state.get("tool_outcomes", [])
            ],
            "draft_version": draft.version,
        }
        run.updated_at = utcnow()
        await session.commit()

        workflow = None
        if checkpointer is not None:
            try:
                workflow = await start_review_workflow(
                    checkpointer, ticket.id, draft.id
                )
            except Exception as exc:  # noqa: BLE001 - persisted ticket stays valid
                log_event(
                    "review_workflow_start_failed",
                    ticket_id=str(ticket.id),
                    error=type(exc).__name__,
                )
                workflow = {"interrupted": False, "error": type(exc).__name__}

        return {
            "ticket_id": str(ticket.id),
            "trace_id": trace_id,
            "route": state.get("route"),
            "intents": state.get("intents", []),
            "risk_flags": state.get("risk_flags", []),
            "requires_human_review": state.get("requires_human_review", False),
            "translated_text": state.get("translated_text"),
            "gaps": state.get("gaps", []),
            "evidence_count": len(state.get("evidence", [])),
            "tool_calls": [
                {
                    "tool_name": outcome["tool_name"],
                    "status": outcome["status"],
                    "step_no": outcome["step_no"],
                }
                for outcome in state.get("tool_outcomes", [])
            ],
            "draft": {
                "id": str(draft.id),
                "version": draft.version,
                "body": draft.body,
                "content_hash": draft.content_hash,
                "structured_facts": draft.structured_facts,
            },
            "agent_run_id": str(run.id),
            "workflow": workflow,
        }