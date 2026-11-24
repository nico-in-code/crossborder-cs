from __future__ import annotations

import uuid
from typing import TypedDict

from langgraph.graph import END, StateGraph
from langgraph.types import Command, interrupt


class ReviewWorkflowState(TypedDict, total=False):
    ticket_id: str
    draft_id: str
    decision: dict | None
    status: str


def build_review_workflow(checkpointer):
    async def await_review(state: ReviewWorkflowState) -> dict:
        decision = interrupt(
            {
                "ticket_id": state["ticket_id"],
                "draft_id": state.get("draft_id"),
                "stage": "awaiting_review",
            }
        )
        return {"decision": decision}

    async def finalize(state: ReviewWorkflowState) -> dict:
        return {"status": "reviewed"}

    graph = StateGraph(ReviewWorkflowState)
    graph.add_node("await_review", await_review)
    graph.add_node("finalize", finalize)
    graph.set_entry_point("await_review")
    graph.add_edge("await_review", "finalize")
    graph.add_edge("finalize", END)
    return graph.compile(checkpointer=checkpointer)


def config_for(ticket_id: uuid.UUID | str) -> dict:
    return {"configurable": {"thread_id": str(ticket_id)}}


async def start_review_workflow(
    checkpointer, ticket_id: uuid.UUID, draft_id: uuid.UUID
) -> dict:
    graph = build_review_workflow(checkpointer)
    state = await graph.ainvoke(
        {"ticket_id": str(ticket_id), "draft_id": str(draft_id)},
        config_for(ticket_id),
    )
    return {
        "interrupted": bool(state.get("__interrupt__")),
        "status": state.get("status"),
        "draft_id": str(draft_id),
    }


async def resume_review_workflow(
    checkpointer, ticket_id: uuid.UUID, decision: dict
) -> dict:
    graph = build_review_workflow(checkpointer)
    state = await graph.ainvoke(Command(resume=decision), config_for(ticket_id))
    return {
        "status": state.get("status"),
        "decision": state.get("decision"),
    }


async def review_workflow_snapshot(checkpointer, ticket_id: uuid.UUID) -> dict:
    graph = build_review_workflow(checkpointer)
    snapshot = await graph.aget_state(config_for(ticket_id))
    return {
        "values": snapshot.values,
        "next": list(snapshot.next),
        "checkpoint_id": snapshot.config.get("configurable", {}).get("checkpoint_id"),
    }