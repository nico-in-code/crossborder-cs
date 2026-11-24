from __future__ import annotations

import hmac
from dataclasses import dataclass
from datetime import datetime, timezone

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Employee, WebSession
from app.security import hash_token

SESSION_COOKIE = "cs_session"


@dataclass
class AuthContext:
    employee: Employee
    session: WebSession


async def get_db(request: Request) -> AsyncSession:
    sessionmaker = request.app.state.sessionmaker
    async with sessionmaker() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


async def get_optional_auth(
    request: Request, db: AsyncSession = Depends(get_db)
) -> AuthContext | None:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    row = (
        await db.execute(
            select(WebSession, Employee)
            .join(Employee, WebSession.employee_id == Employee.id)
            .where(WebSession.token_hash == hash_token(token))
        )
    ).first()
    if row is None:
        return None
    web_session, employee = row
    if web_session.expires_at <= datetime.now(timezone.utc) or not employee.active:
        return None
    context = AuthContext(employee=employee, session=web_session)
    request.state.auth = context
    request.state.employee = employee
    request.state.csrf_token = web_session.csrf_token
    return context


async def require_auth(
    auth: AuthContext | None = Depends(get_optional_auth),
) -> AuthContext:
    if auth is None:
        raise HTTPException(status_code=401, detail="authentication required")
    return auth


async def require_csrf(
    request: Request,
    auth: AuthContext = Depends(require_auth),
) -> AuthContext:
    supplied = request.headers.get("X-CSRF-Token", "")
    if not supplied:
        form = await request.form()
        supplied = str(form.get("_csrf", ""))
    if not hmac.compare_digest(supplied, auth.session.csrf_token):
        raise HTTPException(status_code=403, detail="invalid csrf token")
    return auth