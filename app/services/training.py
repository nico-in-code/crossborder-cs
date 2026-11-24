from __future__ import annotations

import uuid
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.training import TrainingRuntime, run_training_lookup
from app.config import Settings
from app.mcp.context import TRAINING_TOOL_NAMES, RunContext, run_context_token
from app.models import (
    AgentRun,
    Employee,
    EmployeeRole,
    KnowledgeStatus,
    RunStatus,
    Sop,
    SopVersion,
    ToolCall,
    ToolCallStatus,
    TrainingAttempt,
    TrainingCase,
    TrainingStatus,
)
from app.services.permission import accessible_store_ids
from app.trace import log_event, new_trace_id, utcnow

TRAINING_ROLES = (EmployeeRole.agent, EmployeeRole.supervisor)

TRAINING_CHECKLIST: list[dict] = [
    {
        "code": "verify_linkage",
        "label": "核对工单、买家与订单归属",
        "critical": False,
        "keywords": ("归属", "订单", "注文", "紐付"),
    },
    {
        "code": "carrier_investigation",
        "label": "向承运商查询并记录查询编号",
        "critical": True,
        "keywords": ("承运商", "配送会社", "调查", "調査", "照会", "查询"),
    },
    {
        "code": "no_refund_promise",
        "label": "调查完成前不承诺退款",
        "critical": True,
        "keywords": (
            "不承诺",
            "不得承诺",
            "调查完成前",
            "調査完了前",
            "確約しない",
            "返金を確約",
        ),
    },
    {
        "code": "refund_authority",
        "label": "退款由具备退款权限的客服跟进",
        "critical": True,
        "keywords": ("退款权限", "退款権限", "具备权限", "権限"),
    },
    {
        "code": "human_review",
        "label": "外语回复经人工审核后发送",
        "critical": True,
        "keywords": ("人工审核", "审核后发送", "人による確認", "承認"),
    },
    {
        "code": "close_record",
        "label": "记录结论、依据和告知状态后结案",
        "critical": False,
        "keywords": ("结论", "依据", "告知", "結案", "記録"),
    },
]


def _require_training_role(employee: Employee) -> None:
    if employee.role not in TRAINING_ROLES:
        raise HTTPException(status_code=403, detail="permission denied")


def _case_payload(
    case: TrainingCase, sop: Sop, sop_version: SopVersion, *, include_body: bool
) -> dict:
    sop_payload = {
        "code": sop.code,
        "title": sop.title,
        "version": sop_version.version,
        "checksum": sop_version.checksum,
    }
    if include_body:
        sop_payload["body"] = sop_version.body
    return {
        "id": str(case.id),
        "title": case.title,
        "anonymized_content": case.anonymized_content,
        "marketplace": case.marketplace,
        "language": case.language,
        "product_scope": case.product_scope,
        "sop": sop_payload,
    }


def evaluate_answer(answer: str, sop: dict) -> dict:
    normalized = answer.strip().lower()
    matched: list[str] = []
    missing: list[str] = []
    checklist: list[dict] = []
    for item in TRAINING_CHECKLIST:
        hit = any(keyword.lower() in normalized for keyword in item["keywords"])
        checklist.append(
            {"code": item["code"], "label": item["label"], "matched": hit}
        )
        if hit:
            matched.append(item["code"])
        else:
            missing.append(item["code"])
    critical_missing = [
        item["code"]
        for item in TRAINING_CHECKLIST
        if item["critical"] and item["code"] not in matched
    ]
    score = round(100.0 * len(matched) / len(TRAINING_CHECKLIST), 2)
    required_revision = bool(critical_missing) or score < 80.0
    missing_labels = [
        item["label"] for item in TRAINING_CHECKLIST if item["code"] in missing
    ]
    return {
        "verdict": "needs_revision" if required_revision else "passed",
        "score": score,
        "matched": matched,
        "missing": missing,
        "critical_missing": critical_missing,
        "required_revision": required_revision,
        "checklist": checklist,
        "suggestion": (
            "请补充：" + "；".join(missing_labels)
            if missing_labels
            else "覆盖了全部关键步骤。"
        ),
        "sop": {
            "code": sop.get("sop_code"),
            "title": sop.get("sop_title"),
            "version": sop.get("version"),
            "checksum": sop.get("checksum"),
        },
    }


async def list_cases(session: AsyncSession, employee: Employee) -> list[dict]:
    _require_training_role(employee)
    rows = (
        await session.execute(
            select(TrainingCase, Sop, SopVersion)
            .join(SopVersion, SopVersion.id == TrainingCase.sop_version_id)
            .join(Sop, Sop.id == SopVersion.sop_id)
            .where(
                TrainingCase.tenant_id == employee.tenant_id,
                TrainingCase.status == TrainingStatus.approved,
                SopVersion.tenant_id == employee.tenant_id,
                SopVersion.status == KnowledgeStatus.approved,
            )
            .order_by(TrainingCase.title)
        )
    ).all()
    counts = dict(
        (
            await session.execute(
                select(TrainingAttempt.case_id, func.count())
                .where(TrainingAttempt.employee_id == employee.id)
                .group_by(TrainingAttempt.case_id)
            )
        ).all()
    )
    payload = []
    for case, sop, sop_version in rows:
        item = _case_payload(case, sop, sop_version, include_body=False)
        item["attempt_count"] = int(counts.get(case.id, 0))
        payload.append(item)
    return payload


async def get_case(
    session: AsyncSession, employee: Employee, case_id: uuid.UUID
) -> dict:
    _require_training_role(employee)
    row = (
        await session.execute(
            select(TrainingCase, Sop, SopVersion)
            .join(SopVersion, SopVersion.id == TrainingCase.sop_version_id)
            .join(Sop, Sop.id == SopVersion.sop_id)
            .where(
                TrainingCase.id == case_id,
                TrainingCase.tenant_id == employee.tenant_id,
                TrainingCase.status == TrainingStatus.approved,
                SopVersion.tenant_id == employee.tenant_id,
                SopVersion.status == KnowledgeStatus.approved,
            )
        )
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="training case not found")
    case, sop, sop_version = row
    attempts = list(
        (
            await session.scalars(
                select(TrainingAttempt)
                .where(
                    TrainingAttempt.case_id == case.id,
                    TrainingAttempt.employee_id == employee.id,
                )
                .order_by(TrainingAttempt.revision_no)
            )
        ).all()
    )
    payload = _case_payload(case, sop, sop_version, include_body=False)
    payload["attempts"] = [
        {
            "id": str(attempt.id),
            "revision_no": attempt.revision_no,
            "answer": attempt.answer,
            "score": float(attempt.score) if attempt.score is not None else None,
            "feedback": attempt.feedback,
            "created_at": attempt.created_at,
        }
        for attempt in attempts
    ]
    payload["latest_attempt"] = payload["attempts"][-1] if payload["attempts"] else None
    return payload


async def submit_attempt(
    sessionmaker: async_sessionmaker[AsyncSession],
    mcp_sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    employee_id: uuid.UUID,
    case_id: uuid.UUID,
    answer: str,
    previous_attempt_id: uuid.UUID | None = None,
) -> dict:
    answer = answer.strip()
    if not answer:
        raise HTTPException(status_code=422, detail="answer is required")

    async with sessionmaker() as session:
        employee = await session.get(Employee, employee_id)
        if employee is None:
            raise HTTPException(status_code=401, detail="actor not found")
        _require_training_role(employee)
        case = await session.scalar(
            select(TrainingCase).where(
                TrainingCase.id == case_id,
                TrainingCase.tenant_id == employee.tenant_id,
                TrainingCase.status == TrainingStatus.approved,
            )
        )
        if case is None:
            raise HTTPException(status_code=404, detail="training case not found")
        store_ids = await accessible_store_ids(session, employee)
        if not store_ids:
            raise HTTPException(
                status_code=403, detail="store authorization required for training"
            )

        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
            {"key": f"training:{case.id}:{employee.id}"},
        )
        last_revision = (
            await session.scalar(
                select(func.max(TrainingAttempt.revision_no)).where(
                    TrainingAttempt.case_id == case.id,
                    TrainingAttempt.employee_id == employee.id,
                )
            )
            or 0
        )
        if previous_attempt_id is not None:
            previous = await session.get(TrainingAttempt, previous_attempt_id)
            if (
                previous is None
                or previous.case_id != case.id
                or previous.employee_id != employee.id
            ):
                raise HTTPException(status_code=404, detail="attempt not found")
            if previous.revision_no != last_revision:
                raise HTTPException(
                    status_code=409, detail="only the latest attempt can be revised"
                )
        revision_no = last_revision + 1

        run = AgentRun(
            tenant_id=employee.tenant_id,
            ticket_id=None,
            thread_id=f"training:{case.id}:{employee.id}",
            graph="training",
            status=RunStatus.running,
            state_summary={},
            trace_id=new_trace_id(),
        )
        session.add(run)
        await session.flush()
        run_id = run.id
        run_trace = run.trace_id
        tenant_id = employee.tenant_id
        role = employee.role.value
        case_title = case.title
        await session.commit()

    context = RunContext(
        tenant_id=tenant_id,
        store_ids=store_ids,
        employee_id=str(employee_id),
        role=role,
        allowed_tools=TRAINING_TOOL_NAMES,
        ticket_id=None,
        agent_run_id=str(run_id),
        graph="training",
    )
    runtime = TrainingRuntime(
        settings=settings,
        sessionmaker=mcp_sessionmaker,
        context_token=run_context_token(settings, context),
        tenant_id=tenant_id,
        case_id=str(case_id),
    )

    try:
        lookup = await run_training_lookup(runtime)
    except Exception as exc:  # noqa: BLE001 - persist a failed run for audit
        async with sessionmaker() as session:
            run = await session.get(AgentRun, run_id)
            if run is not None:
                run.status = RunStatus.failed
                run.state_summary = {"error": type(exc).__name__}
                await session.commit()
        raise HTTPException(status_code=503, detail="training tools unavailable") from exc

    sop = lookup["sop"]
    failure = None
    if lookup["case"] is None:
        failure = 409
    elif sop is None:
        failure = 503

    if failure is not None:
        async with sessionmaker() as session:
            run = await session.get(AgentRun, run_id)
            if run is not None:
                for outcome in lookup["tool_outcomes"]:
                    session.add(
                        ToolCall(
                            tenant_id=tenant_id,
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
                run.status = RunStatus.failed
                run.state_summary = {"gaps": lookup["gaps"]}
                await session.commit()
        raise HTTPException(
            status_code=failure,
            detail="; ".join(lookup["gaps"]) or "training lookup failed",
        )

    feedback = evaluate_answer(answer, sop)
    score = Decimal(str(feedback["score"]))
    tool_names = [outcome["tool_name"] for outcome in lookup["tool_outcomes"]]

    async with sessionmaker() as session:
        attempt = TrainingAttempt(
            tenant_id=tenant_id,
            case_id=case_id,
            employee_id=employee_id,
            revision_no=revision_no,
            answer=answer,
            feedback=feedback,
            score=score,
        )
        session.add(attempt)
        await session.flush()

        run = await session.get(AgentRun, run_id)
        if run is None:
            raise HTTPException(status_code=500, detail="training run vanished")
        for outcome in lookup["tool_outcomes"]:
            session.add(
                ToolCall(
                    tenant_id=tenant_id,
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
        run.state_summary = {
            "case_id": str(case_id),
            "case_title": case_title,
            "revision_no": revision_no,
            "verdict": feedback["verdict"],
            "score": feedback["score"],
            "missing": feedback["missing"],
            "primary_error": "missing_critical_step"
            if feedback["critical_missing"]
            else None,
            "sop_version": sop.get("version"),
            "sop_checksum": sop.get("checksum"),
            "tools": tool_names,
        }
        run.updated_at = utcnow()
        await session.commit()
        attempt_id = attempt.id
        attempt_created = attempt.created_at

    log_event(
        "training_attempt_saved",
        trace_id=run_trace,
        case_id=str(case_id),
        revision_no=revision_no,
        verdict=feedback["verdict"],
        score=feedback["score"],
        tools=len(tool_names),
    )

    return {
        "case_id": str(case_id),
        "attempt_id": str(attempt_id),
        "revision_no": revision_no,
        "answer": answer,
        "score": feedback["score"],
        "feedback": feedback,
        "created_at": attempt_created,
        "agent_run_id": str(run_id),
        "trace_id": run_trace,
        "trajectory": {
            "graph": "training",
            "tools": [
                {
                    "tool_name": outcome["tool_name"],
                    "status": outcome["status"],
                    "step_no": outcome["step_no"],
                }
                for outcome in lookup["tool_outcomes"]
            ],
            "sop": feedback["sop"],
        },
    }