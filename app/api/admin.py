from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import AuthContext, get_db, require_auth, require_csrf
from app.schemas import EmployeeUpdatePayload, StoreGrantPayload
from app.services.admin import (
    add_store_grant,
    list_audits,
    list_employees,
    revoke_store_grant,
    update_employee,
)
from app.services.permission import require_system_admin

router = APIRouter(prefix="/api/admin", tags=["admin"])


def _require_admin(auth: AuthContext) -> None:
    require_system_admin(auth.employee)


@router.get("/employees")
async def employees(
    auth: AuthContext = Depends(require_auth), db: AsyncSession = Depends(get_db)
):
    _require_admin(auth)
    tenant_id = auth.employee.tenant_id
    rows, stores = await list_employees(db, tenant_id)
    return {
        "employees": rows,
        "stores": stores,
        "audits": await list_audits(db, tenant_id),
    }


@router.patch("/employees/{employee_id}")
async def patch_employee(
    employee_id: uuid.UUID,
    payload: EmployeeUpdatePayload,
    auth: AuthContext = Depends(require_csrf),
    db: AsyncSession = Depends(get_db),
):
    _require_admin(auth)
    return await update_employee(
        db,
        auth.employee,
        employee_id,
        role=payload.role,
        refund_authority=payload.refund_authority,
        active=payload.active,
    )


@router.post("/employees/{employee_id}/grants")
async def create_grant(
    employee_id: uuid.UUID,
    payload: StoreGrantPayload,
    auth: AuthContext = Depends(require_csrf),
    db: AsyncSession = Depends(get_db),
):
    _require_admin(auth)
    try:
        store_id = uuid.UUID(payload.store_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="store_id must be a UUID") from exc
    return await add_store_grant(db, auth.employee, employee_id, store_id)


@router.delete("/employees/{employee_id}/grants/{store_id}")
async def delete_grant(
    employee_id: uuid.UUID,
    store_id: uuid.UUID,
    auth: AuthContext = Depends(require_csrf),
    db: AsyncSession = Depends(get_db),
):
    _require_admin(auth)
    return await revoke_store_grant(db, auth.employee, employee_id, store_id)