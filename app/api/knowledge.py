from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import AuthContext, get_db, require_auth, require_csrf
from app.models import IndexSnapshot
from app.schemas import IndexPublishPayload, IndexRollbackPayload
from app.services.index_publish import (
    IndexPublishError,
    active_snapshot,
    publish_index,
    rollback_index,
)

router = APIRouter(prefix="/api/knowledge/index", tags=["knowledge-index"])


def _require_index_admin(auth: AuthContext) -> None:
    if auth.employee.role.value not in {"knowledge_admin", "system_admin"}:
        raise HTTPException(status_code=403, detail="knowledge admin permission required")


def _snapshot(snapshot: IndexSnapshot) -> dict:
    return {
        "id": str(snapshot.id),
        "status": snapshot.status,
        "collection": snapshot.collection,
        "version_ids": list(snapshot.version_ids or []),
        "previous_snapshot_id": (
            str(snapshot.previous_snapshot_id) if snapshot.previous_snapshot_id else None
        ),
        "activated_at": snapshot.activated_at,
        "error": snapshot.error,
        "created_at": snapshot.created_at,
    }


@router.get("")
async def index_status(
    auth: AuthContext = Depends(require_auth), db: AsyncSession = Depends(get_db)
):
    active = await active_snapshot(db, auth.employee.tenant_id)
    snapshots = list(
        await db.scalars(
            select(IndexSnapshot)
            .where(IndexSnapshot.tenant_id == auth.employee.tenant_id)
            .order_by(IndexSnapshot.created_at.desc())
            .limit(10)
        )
    )
    return {
        "active": _snapshot(active) if active is not None else None,
        "snapshots": [_snapshot(snapshot) for snapshot in snapshots],
    }


@router.post("/publish")
async def publish(
    payload: IndexPublishPayload,
    request: Request,
    auth: AuthContext = Depends(require_csrf),
    db: AsyncSession = Depends(get_db),
):
    _require_index_admin(auth)
    try:
        return await publish_index(
            db,
            request.app.state.vector_index,
            auth.employee.tenant_id,
            payload.version_ids,
            collection=payload.collection,
        )
    except (IndexPublishError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/rollback")
async def rollback(
    payload: IndexRollbackPayload,
    auth: AuthContext = Depends(require_csrf),
    db: AsyncSession = Depends(get_db),
):
    _require_index_admin(auth)
    try:
        return await rollback_index(
            db, auth.employee.tenant_id, uuid.UUID(payload.snapshot_id)
        )
    except (IndexPublishError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc