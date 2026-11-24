from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.datastructures import UploadFile

from app.deps import AuthContext, get_db, require_auth, require_csrf
from app.services.attachments import (
    AttachmentError,
    AttachmentPathError,
    AttachmentTooLarge,
    load_attachment,
    ocr_attachment,
    save_attachment,
)

router = APIRouter(prefix="/api/tickets/{ticket_id}/attachments", tags=["attachments"])


@router.post("")
async def upload(
    ticket_id: uuid.UUID,
    request: Request,
    auth: AuthContext = Depends(require_csrf),
    db: AsyncSession = Depends(get_db),
):
    form = await request.form()
    upload_file = form.get("file")
    if not isinstance(upload_file, UploadFile):
        raise HTTPException(status_code=422, detail="multipart field 'file' is required")
    data = await upload_file.read()
    try:
        result = await save_attachment(
            db,
            request.app.state.settings,
            auth.employee,
            ticket_id,
            upload_file.filename,
            upload_file.content_type or "application/octet-stream",
            data,
        )
    except AttachmentTooLarge as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    except AttachmentPathError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except AttachmentError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    result["url"] = f"/api/tickets/{ticket_id}/attachments/{result['id']}"
    return result


@router.get("/{attachment_id}")
async def download(
    ticket_id: uuid.UUID,
    attachment_id: uuid.UUID,
    request: Request,
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    attachment, path = await load_attachment(
        db,
        request.app.state.settings,
        auth.employee,
        ticket_id,
        attachment_id,
    )
    disposition = "attachment" if request.query_params.get("download") else "inline"
    return FileResponse(
        path,
        media_type=attachment.content_type,
        filename=attachment.filename,
        content_disposition_type=disposition,
    )


@router.post("/{attachment_id}/ocr")
async def run_ocr(
    ticket_id: uuid.UUID,
    attachment_id: uuid.UUID,
    request: Request,
    auth: AuthContext = Depends(require_csrf),
    db: AsyncSession = Depends(get_db),
):
    return await ocr_attachment(
        db,
        request.app.state.settings,
        request.app.state.ocr_adapter,
        auth.employee,
        ticket_id,
        attachment_id,
    )