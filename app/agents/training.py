from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.mcp.client import ScopedToolClient, open_training_session
from app.models import ToolCallStatus


@dataclass
class TrainingRuntime:
    settings: Settings
    sessionmaker: async_sessionmaker[AsyncSession]
    context_token: str
    tenant_id: uuid.UUID
    case_id: str


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


async def run_training_lookup(runtime: TrainingRuntime) -> dict:
    tool_outcomes: list[dict] = []
    gaps: list[str] = []
    async with open_training_session(
        runtime.settings, runtime.sessionmaker
    ) as mcp_session:
        client = ScopedToolClient(runtime.settings)

        case_outcome = await client.call(
            mcp_session,
            runtime.context_token,
            "training_case_get",
            {"case_id": runtime.case_id},
        )
        tool_outcomes.append(_outcome_dict(case_outcome))
        if case_outcome.status is not ToolCallStatus.ok:
            gaps.append("培训案例读取失败，无法开始本次练习")
            return {
                "case": None,
                "sop": None,
                "tool_outcomes": tool_outcomes,
                "gaps": gaps,
            }

        sop_outcome = await client.call(
            mcp_session,
            runtime.context_token,
            "training_sop_get",
            {"case_id": runtime.case_id},
        )
        tool_outcomes.append(_outcome_dict(sop_outcome))
        if sop_outcome.status is not ToolCallStatus.ok:
            gaps.append("培训案例绑定的 SOP 版本不可用，请联系知识管理员")
            return {
                "case": case_outcome.data,
                "sop": None,
                "tool_outcomes": tool_outcomes,
                "gaps": gaps,
            }

    return {
        "case": case_outcome.data,
        "sop": sop_outcome.data,
        "tool_outcomes": tool_outcomes,
        "gaps": gaps,
    }