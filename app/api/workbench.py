from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.workflow import review_workflow_snapshot
from app.api.integrations import build_status
from app.deps import SESSION_COOKIE, AuthContext, get_db, get_optional_auth
from app.services.admin import (
    ROLE_LABELS,
    list_audits,
    list_employees,
)
from app.services.auth import authenticate, create_session, revoke_session
from app.services.index_publish import (
    active_snapshot,
    approved_versions,
    recent_snapshots,
)
from app.services.ticket import get_ticket_detail, list_tickets
from app.services.training import get_case, list_cases

router = APIRouter(tags=["workbench"])


def _templates(request: Request):
    return request.app.state.templates


@router.get("/", response_class=HTMLResponse)
async def root(auth: AuthContext | None = Depends(get_optional_auth)):
    if auth is None:
        return RedirectResponse("/login", status_code=303)
    return RedirectResponse("/tickets", status_code=303)


@router.get("/login", response_class=HTMLResponse)
async def login_page(
    request: Request,
    auth: AuthContext | None = Depends(get_optional_auth),
    error: str | None = None,
):
    if auth is not None:
        return RedirectResponse("/tickets", status_code=303)
    return _templates(request).TemplateResponse(
        request=request,
        name="login.html",
        context={"employee": None, "error": error, "csrf_token": ""},
    )


@router.post("/login", response_class=HTMLResponse)
async def login_submit(request: Request, db: AsyncSession = Depends(get_db)):
    form = await request.form()
    email = str(form.get("email", ""))
    password = str(form.get("password", ""))
    try:
        employee = await authenticate(db, email, password)
    except HTTPException:
        return _templates(request).TemplateResponse(
            request=request,
            name="login.html",
            context={
                "employee": None,
                "error": "邮箱或密码不正确",
                "csrf_token": "",
            },
            status_code=401,
        )
    token, _web_session = await create_session(db, employee)
    response = RedirectResponse("/tickets", status_code=303)
    response.set_cookie(
        SESSION_COOKIE,
        token,
        httponly=True,
        samesite="lax",
        secure=request.app.state.settings.app_env == "production",
        max_age=8 * 3600,
        path="/",
    )
    return response


@router.post("/logout")
async def logout_submit(
    request: Request,
    db: AsyncSession = Depends(get_db),
    auth: AuthContext | None = Depends(get_optional_auth),
):
    if auth is None:
        return RedirectResponse("/login", status_code=303)
    form = await request.form()
    if str(form.get("_csrf", "")) != auth.session.csrf_token:
        raise HTTPException(status_code=403, detail="invalid csrf token")
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        await revoke_session(db, token)
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


@router.get("/tickets", response_class=HTMLResponse)
async def ticket_list_page(
    request: Request,
    db: AsyncSession = Depends(get_db),
    auth: AuthContext | None = Depends(get_optional_auth),
):
    if auth is None:
        return RedirectResponse("/login", status_code=303)
    rows = await list_tickets(db, auth.employee)
    return _templates(request).TemplateResponse(
        request=request,
        name="tickets.html",
        context={
            "employee": auth.employee,
            "csrf_token": auth.session.csrf_token,
            "tickets": rows,
        },
    )


@router.get("/training", response_class=HTMLResponse)
async def training_page(
    request: Request,
    db: AsyncSession = Depends(get_db),
    auth: AuthContext | None = Depends(get_optional_auth),
):
    if auth is None:
        return RedirectResponse("/login", status_code=303)
    try:
        cases = await list_cases(db, auth.employee)
    except HTTPException as exc:
        return _templates(request).TemplateResponse(
            request=request,
            name="error.html",
            context={
                "employee": auth.employee,
                "csrf_token": auth.session.csrf_token,
                "status_code": exc.status_code,
                "message": "当前账号没有培训入口权限",
            },
            status_code=exc.status_code,
        )
    return _templates(request).TemplateResponse(
        request=request,
        name="training.html",
        context={
            "employee": auth.employee,
            "csrf_token": auth.session.csrf_token,
            "cases": cases,
        },
    )


@router.get("/training/{case_id}", response_class=HTMLResponse)
async def training_case_page(
    case_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    auth: AuthContext | None = Depends(get_optional_auth),
):
    if auth is None:
        return RedirectResponse("/login", status_code=303)
    try:
        case = await get_case(db, auth.employee, case_id)
    except HTTPException as exc:
        return _templates(request).TemplateResponse(
            request=request,
            name="error.html",
            context={
                "employee": auth.employee,
                "csrf_token": auth.session.csrf_token,
                "status_code": exc.status_code,
                "message": (
                    "培训案例不存在或不属于当前租户"
                    if exc.status_code == 404
                    else "当前账号没有培训入口权限"
                ),
            },
            status_code=exc.status_code,
        )
    return _templates(request).TemplateResponse(
        request=request,
        name="training_case.html",
        context={
            "employee": auth.employee,
            "csrf_token": auth.session.csrf_token,
            "case": case,
        },
    )


@router.get("/tickets/{ticket_id}", response_class=HTMLResponse)
async def ticket_detail_page(
    ticket_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    auth: AuthContext | None = Depends(get_optional_auth),
):
    if auth is None:
        return RedirectResponse("/login", status_code=303)
    try:
        detail = await get_ticket_detail(db, auth.employee, ticket_id)
    except HTTPException as exc:
        return _templates(request).TemplateResponse(
            request=request,
            name="error.html",
            context={
                "employee": auth.employee,
                "csrf_token": auth.session.csrf_token,
                "status_code": exc.status_code,
                "message": (
                    "无权访问该工单或工单不存在"
                    if exc.status_code == 404
                    else "当前账号没有执行该操作的权限"
                ),
            },
            status_code=exc.status_code,
        )
    workflow = None
    checkpointer = getattr(request.app.state, "checkpointer", None)
    if checkpointer is not None:
        try:
            workflow = await review_workflow_snapshot(checkpointer, ticket_id)
        except Exception:
            workflow = None
    return _templates(request).TemplateResponse(
        request=request,
        name="ticket_detail.html",
        context={
            "employee": auth.employee,
            "csrf_token": auth.session.csrf_token,
            "workflow": workflow,
            "attachment_max_bytes": request.app.state.settings.attachment_max_bytes,
            **detail,
        },
    )


def _render_forbidden(request: Request, auth: AuthContext, message: str):
    return _templates(request).TemplateResponse(
        request=request,
        name="error.html",
        context={
            "employee": auth.employee,
            "csrf_token": auth.session.csrf_token,
            "status_code": 403,
            "message": message,
        },
        status_code=403,
    )


@router.get("/knowledge", response_class=HTMLResponse)
async def knowledge_page(
    request: Request,
    db: AsyncSession = Depends(get_db),
    auth: AuthContext | None = Depends(get_optional_auth),
):
    if auth is None:
        return RedirectResponse("/login", status_code=303)
    if auth.employee.role.value not in {"knowledge_admin", "system_admin"}:
        return _render_forbidden(request, auth, "当前账号没有知识索引管理权限")
    return _templates(request).TemplateResponse(
        request=request,
        name="knowledge.html",
        context={
            "employee": auth.employee,
            "csrf_token": auth.session.csrf_token,
            "active": await active_snapshot(db, auth.employee.tenant_id),
            "snapshots": await recent_snapshots(db, auth.employee.tenant_id),
            "versions": await approved_versions(db, auth.employee.tenant_id),
        },
    )


@router.get("/integrations", response_class=HTMLResponse)
async def integrations_page(
    request: Request,
    auth: AuthContext | None = Depends(get_optional_auth),
):
    if auth is None:
        return RedirectResponse("/login", status_code=303)
    if auth.employee.role.value != "system_admin":
        return _render_forbidden(request, auth, "当前账号没有集成状态查看权限")
    return _templates(request).TemplateResponse(
        request=request,
        name="integrations.html",
        context={
            "employee": auth.employee,
            "csrf_token": auth.session.csrf_token,
            "status": build_status(request.app.state.settings),
        },
    )


@router.get("/employees", response_class=HTMLResponse)
async def employees_page(
    request: Request,
    db: AsyncSession = Depends(get_db),
    auth: AuthContext | None = Depends(get_optional_auth),
):
    if auth is None:
        return RedirectResponse("/login", status_code=303)
    if auth.employee.role.value != "system_admin":
        return _render_forbidden(request, auth, "当前账号没有员工权限管理权限")
    employees, stores = await list_employees(db, auth.employee.tenant_id)
    return _templates(request).TemplateResponse(
        request=request,
        name="employees.html",
        context={
            "employee": auth.employee,
            "csrf_token": auth.session.csrf_token,
            "employees": employees,
            "stores": stores,
            "audits": await list_audits(db, auth.employee.tenant_id),
            "role_labels": ROLE_LABELS,
            "ticket_roles": ["agent", "supervisor"],
        },
    )