from __future__ import annotations

import uuid

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.models import (
    AdminAudit,
    Employee,
    EmployeeRole,
    Store,
    StoreGrant,
    Tenant,
)
from app.services.permission import require_system_admin

ROLE_LABELS = {
    "agent": "客服",
    "supervisor": "主管",
    "knowledge_admin": "知识管理员",
    "system_admin": "系统管理员",
}

TICKET_ROLES = {EmployeeRole.agent, EmployeeRole.supervisor}


def _fmt_change(field: str, value: object) -> str:
    if field == "active":
        return "启用" if value else "停用"
    if field == "refund_authority":
        return "有" if value else "无"
    if field == "role":
        return ROLE_LABELS.get(str(value), str(value))
    return str(value)


def _audit_summary(action: str, payload: dict) -> str:
    if action == "employee_updated":
        parts = []
        for field, change in (payload.get("changes") or {}).items():
            label = {"role": "角色", "refund_authority": "退款权限", "active": "状态"}.get(
                field, field
            )
            parts.append(
                f"{label} {_fmt_change(field, change.get('from'))} → "
                f"{_fmt_change(field, change.get('to'))}"
            )
        return "；".join(parts)
    if action in {"store_grant_added", "store_grant_removed"}:
        return f"店铺 {payload.get('store_code', '')}"
    return ""


def _store_payload(store: Store) -> dict:
    return {
        "id": str(store.id),
        "code": store.code,
        "name": store.name,
        "enabled": store.enabled,
    }


def _employee_payload(employee: Employee, stores: list[Store]) -> dict:
    return {
        "id": str(employee.id),
        "email": employee.email,
        "display_name": employee.display_name,
        "role": employee.role.value,
        "role_label": ROLE_LABELS.get(employee.role.value, employee.role.value),
        "refund_authority": employee.refund_authority,
        "active": employee.active,
        "created_at": employee.created_at,
        "stores": [_store_payload(store) for store in stores],
    }


async def _tenant_employee(
    session: AsyncSession, tenant_id: uuid.UUID, employee_id: uuid.UUID
) -> Employee:
    employee = await session.get(Employee, employee_id)
    if employee is None or employee.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="员工不存在")
    return employee


async def _grants_by_employee(
    session: AsyncSession, tenant_id: uuid.UUID
) -> dict[uuid.UUID, list[Store]]:
    stores_by_id = {
        store.id: store
        for store in await session.scalars(select(Store).where(Store.tenant_id == tenant_id))
    }
    grants = await session.scalars(
        select(StoreGrant)
        .where(StoreGrant.tenant_id == tenant_id)
        .order_by(StoreGrant.created_at)
    )
    result: dict[uuid.UUID, list[Store]] = {}
    for grant in grants:
        store = stores_by_id.get(grant.store_id)
        if store is not None:
            result.setdefault(grant.employee_id, []).append(store)
    for stores in result.values():
        stores.sort(key=lambda item: item.code)
    return result


async def list_employees(
    session: AsyncSession, tenant_id: uuid.UUID
) -> tuple[list[dict], list[dict]]:
    employees = list(
        await session.scalars(
            select(Employee)
            .where(Employee.tenant_id == tenant_id)
            .order_by(Employee.created_at, Employee.email)
        )
    )
    stores_by_employee = await _grants_by_employee(session, tenant_id)
    stores = [
        _store_payload(store)
        for store in await session.scalars(
            select(Store).where(Store.tenant_id == tenant_id).order_by(Store.code)
        )
    ]
    return (
        [
            _employee_payload(employee, stores_by_employee.get(employee.id, []))
            for employee in employees
        ],
        stores,
    )


async def list_audits(
    session: AsyncSession, tenant_id: uuid.UUID, limit: int = 20
) -> list[dict]:
    actor = aliased(Employee, name="actor")
    target = aliased(Employee, name="target")
    rows = await session.execute(
        select(AdminAudit, actor.display_name, target.display_name)
        .outerjoin(actor, AdminAudit.actor_id == actor.id)
        .outerjoin(target, AdminAudit.target_employee_id == target.id)
        .where(AdminAudit.tenant_id == tenant_id)
        .order_by(AdminAudit.created_at.desc())
        .limit(limit)
    )
    return [
        {
            "id": str(audit.id),
            "created_at": audit.created_at,
            "action": audit.action,
            "actor_name": actor_name or "—",
            "target_name": target_name or "—",
            "summary": _audit_summary(audit.action, audit.payload),
        }
        for audit, actor_name, target_name in rows
    ]


async def _active_admin_count(session: AsyncSession, tenant_id: uuid.UUID) -> int:
    return (
        await session.scalar(
            select(func.count())
            .select_from(Employee)
            .where(
                Employee.tenant_id == tenant_id,
                Employee.role == EmployeeRole.system_admin,
                Employee.active.is_(True),
            )
        )
    ) or 0


def _lock_tenant(session: AsyncSession, tenant_id: uuid.UUID):
    return session.scalar(select(Tenant.id).where(Tenant.id == tenant_id).with_for_update())


async def update_employee(
    session: AsyncSession,
    actor: Employee,
    employee_id: uuid.UUID,
    *,
    role: EmployeeRole | None = None,
    refund_authority: bool | None = None,
    active: bool | None = None,
) -> dict:
    require_system_admin(actor)
    await _lock_tenant(session, actor.tenant_id)
    target = await _tenant_employee(session, actor.tenant_id, employee_id)
    changes: dict[str, dict] = {}

    if role is not None and role != target.role:
        if target.id == actor.id:
            raise HTTPException(status_code=409, detail="不能修改自己的角色")
        if (
            target.role == EmployeeRole.system_admin
            and target.active
            and await _active_admin_count(session, actor.tenant_id) <= 1
        ):
            raise HTTPException(status_code=409, detail="不能移除最后一个系统管理员")
        changes["role"] = {"from": target.role.value, "to": role.value}
        target.role = role

    if active is not None and active != target.active:
        if not active and target.id == actor.id:
            raise HTTPException(status_code=409, detail="不能停用自己")
        if (
            not active
            and target.role == EmployeeRole.system_admin
            and target.active
            and await _active_admin_count(session, actor.tenant_id) <= 1
        ):
            raise HTTPException(status_code=409, detail="不能停用最后一个系统管理员")
        changes["active"] = {"from": target.active, "to": active}
        target.active = active

    if refund_authority is not None and refund_authority != target.refund_authority:
        changes["refund_authority"] = {
            "from": target.refund_authority,
            "to": refund_authority,
        }
        target.refund_authority = refund_authority

    if changes:
        session.add(
            AdminAudit(
                tenant_id=actor.tenant_id,
                actor_id=actor.id,
                target_employee_id=target.id,
                action="employee_updated",
                payload={"changes": changes},
            )
        )
        await session.commit()

    stores_by_employee = await _grants_by_employee(session, actor.tenant_id)
    return _employee_payload(target, stores_by_employee.get(target.id, []))


async def add_store_grant(
    session: AsyncSession,
    actor: Employee,
    employee_id: uuid.UUID,
    store_id: uuid.UUID,
) -> dict:
    require_system_admin(actor)
    target = await _tenant_employee(session, actor.tenant_id, employee_id)
    if target.role not in TICKET_ROLES:
        raise HTTPException(status_code=409, detail="店铺授权仅适用于客服或主管")
    store = await session.get(Store, store_id)
    if store is None or store.tenant_id != actor.tenant_id:
        raise HTTPException(status_code=404, detail="店铺不存在")
    existing = await session.scalar(
        select(StoreGrant).where(
            StoreGrant.employee_id == target.id, StoreGrant.store_id == store.id
        )
    )
    if existing is not None:
        return {
            "employee_id": str(target.id),
            "store": _store_payload(store),
            "idempotent": True,
        }
    session.add(
        StoreGrant(
            tenant_id=actor.tenant_id,
            employee_id=target.id,
            store_id=store.id,
        )
    )
    session.add(
        AdminAudit(
            tenant_id=actor.tenant_id,
            actor_id=actor.id,
            target_employee_id=target.id,
            action="store_grant_added",
            payload={"store_id": str(store.id), "store_code": store.code},
        )
    )
    await session.commit()
    return {"employee_id": str(target.id), "store": _store_payload(store), "idempotent": False}


async def revoke_store_grant(
    session: AsyncSession,
    actor: Employee,
    employee_id: uuid.UUID,
    store_id: uuid.UUID,
) -> dict:
    require_system_admin(actor)
    target = await _tenant_employee(session, actor.tenant_id, employee_id)
    grant = await session.scalar(
        select(StoreGrant).where(
            StoreGrant.tenant_id == actor.tenant_id,
            StoreGrant.employee_id == target.id,
            StoreGrant.store_id == store_id,
        )
    )
    if grant is None:
        raise HTTPException(status_code=404, detail="授权不存在")
    store = await session.get(Store, store_id)
    await session.delete(grant)
    session.add(
        AdminAudit(
            tenant_id=actor.tenant_id,
            actor_id=actor.id,
            target_employee_id=target.id,
            action="store_grant_removed",
            payload={
                "store_id": str(store_id),
                "store_code": store.code if store is not None else "",
            },
        )
    )
    await session.commit()
    return {"employee_id": str(target.id), "store_id": str(store_id), "revoked": True}