from __future__ import annotations

import uuid

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Employee, EmployeeRole, StoreGrant, Ticket

TICKET_ROLES = (EmployeeRole.agent, EmployeeRole.supervisor)


async def accessible_store_ids(session: AsyncSession, employee: Employee) -> set[uuid.UUID]:
    if employee.role not in TICKET_ROLES:
        return set()
    rows = await session.scalars(
        select(StoreGrant.store_id).where(
            StoreGrant.employee_id == employee.id,
            StoreGrant.tenant_id == employee.tenant_id,
        )
    )
    return set(rows)


def require_role(employee: Employee, *roles: EmployeeRole) -> None:
    if employee.role not in roles:
        raise HTTPException(status_code=403, detail="permission denied")


async def get_ticket_for_employee(
    session: AsyncSession,
    employee: Employee,
    ticket_id: uuid.UUID,
    action: str = "read",
) -> Ticket:
    ticket = await session.get(Ticket, ticket_id)
    if ticket is None or ticket.tenant_id != employee.tenant_id:
        raise HTTPException(status_code=404, detail="ticket not found")
    if employee.role not in TICKET_ROLES:
        raise HTTPException(status_code=403, detail="permission denied")
    grants = await accessible_store_ids(session, employee)
    if ticket.store_id not in grants:
        raise HTTPException(status_code=404, detail="ticket not found")
    if action == "assign" and employee.role != EmployeeRole.supervisor:
        raise HTTPException(status_code=403, detail="supervisor permission required")
    if action in {"refund", "close"} and not employee.refund_authority:
        raise HTTPException(status_code=403, detail="refund authority required")
    return ticket


async def can_manage_knowledge(session: AsyncSession, employee: Employee) -> bool:
    return employee.role == EmployeeRole.knowledge_admin


def require_system_admin(employee: Employee) -> None:
    if employee.role != EmployeeRole.system_admin:
        raise HTTPException(status_code=403, detail="system administrator permission required")