from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import SESSION_COOKIE, AuthContext, get_db, require_auth, require_csrf
from app.schemas import LoginPayload
from app.services.auth import authenticate, create_session, revoke_session

router = APIRouter(prefix="/api/auth", tags=["auth"])


def _cookie_secure(request: Request) -> bool:
    return request.app.state.settings.app_env == "production"


@router.post("/login")
async def login(request: Request, response: Response, db: AsyncSession = Depends(get_db)):
    content_type = request.headers.get("content-type", "")
    try:
        if "application/json" in content_type:
            payload = LoginPayload.model_validate(await request.json())
        else:
            form = await request.form()
            payload = LoginPayload(
                email=str(form.get("email", "")), password=str(form.get("password", ""))
            )
    except (ValidationError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="email and password are required") from exc

    employee = await authenticate(db, payload.email, payload.password)
    token, web_session = await create_session(db, employee)
    response.set_cookie(
        SESSION_COOKIE,
        token,
        httponly=True,
        samesite="lax",
        secure=_cookie_secure(request),
        max_age=8 * 3600,
        path="/",
    )
    return {
        "employee": {
            "id": str(employee.id),
            "email": employee.email,
            "display_name": employee.display_name,
            "role": employee.role.value,
            "refund_authority": employee.refund_authority,
        },
        "csrf_token": web_session.csrf_token,
    }


@router.post("/logout")
async def logout(
    request: Request,
    response: Response,
    auth: AuthContext = Depends(require_csrf),
    db: AsyncSession = Depends(get_db),
):
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        await revoke_session(db, token)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}


@router.get("/me")
async def me(auth: AuthContext = Depends(require_auth)):
    employee = auth.employee
    return {
        "employee": {
            "id": str(employee.id),
            "email": employee.email,
            "display_name": employee.display_name,
            "role": employee.role.value,
            "refund_authority": employee.refund_authority,
        },
        "csrf_token": auth.session.csrf_token,
    }