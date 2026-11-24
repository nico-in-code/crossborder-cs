from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import AuthContext, get_db, require_auth, require_csrf
from app.schemas import TrainingAttemptPayload
from app.services.training import get_case, list_cases, submit_attempt

router = APIRouter(prefix="/api/training", tags=["training"])


@router.get("/cases")
async def cases(
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    return {"items": await list_cases(db, auth.employee)}


@router.get("/cases/{case_id}")
async def case_detail(
    case_id: uuid.UUID,
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    return await get_case(db, auth.employee, case_id)


@router.get("/cases/{case_id}/attempts")
async def case_attempts(
    case_id: uuid.UUID,
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    detail = await get_case(db, auth.employee, case_id)
    return {"items": detail["attempts"]}


@router.post("/cases/{case_id}/attempts")
async def create_attempt(
    case_id: uuid.UUID,
    payload: TrainingAttemptPayload,
    request: Request,
    auth: AuthContext = Depends(require_csrf),
):
    return await submit_attempt(
        request.app.state.sessionmaker,
        request.app.state.mcp_sessionmaker,
        request.app.state.settings,
        auth.employee.id,
        case_id,
        payload.answer,
    )


@router.post("/cases/{case_id}/attempts/{attempt_id}/revise")
async def revise_attempt(
    case_id: uuid.UUID,
    attempt_id: uuid.UUID,
    payload: TrainingAttemptPayload,
    request: Request,
    auth: AuthContext = Depends(require_csrf),
):
    return await submit_attempt(
        request.app.state.sessionmaker,
        request.app.state.mcp_sessionmaker,
        request.app.state.settings,
        auth.employee.id,
        case_id,
        payload.answer,
        previous_attempt_id=attempt_id,
    )