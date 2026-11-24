from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Employee, WebSession
from app.security import hash_password, hash_token, new_token, verify_password


async def authenticate(
    session: AsyncSession, email: str, password: str
) -> Employee:
    employee = (
        await session.execute(
            select(Employee).where(func.lower(Employee.email) == email.strip().lower())
        )
    ).scalar_one_or_none()
    if (
        employee is None
        or not employee.active
        or not verify_password(password, employee.password_hash)
    ):
        raise HTTPException(status_code=401, detail="invalid email or password")
    return employee


async def create_session(
    session: AsyncSession, employee: Employee, hours: int = 8
) -> tuple[str, WebSession]:
    token = new_token()
    csrf = new_token(24)
    web_session = WebSession(
        employee_id=employee.id,
        token_hash=hash_token(token),
        csrf_token=csrf,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=hours),
    )
    session.add(web_session)
    await session.commit()
    return token, web_session


async def revoke_session(session: AsyncSession, token: str) -> None:
    web_session = (
        await session.execute(
            select(WebSession).where(WebSession.token_hash == hash_token(token))
        )
    ).scalar_one_or_none()
    if web_session is not None:
        await session.delete(web_session)
        await session.commit()


async def ensure_seed_password(session: AsyncSession, employee: Employee, password: str) -> None:
    employee.password_hash = hash_password(password)
    await session.commit()