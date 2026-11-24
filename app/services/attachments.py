from __future__ import annotations

import hashlib
import os
import uuid
from pathlib import Path

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.integrations import NotConfigured
from app.adapters.ocr import OcrAdapter
from app.config import Settings
from app.models import Attachment, Employee
from app.services.permission import get_ticket_for_employee

ALLOWED_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".webp",
    ".pdf",
    ".txt",
}


class AttachmentError(ValueError):
    pass


class AttachmentTooLarge(AttachmentError):
    pass


class AttachmentPathError(AttachmentError):
    pass


def sanitize_filename(filename: str | None) -> str:
    name = (filename or "attachment").replace("\\", "/").split("/")[-1].strip()
    name = "".join(
        character
        for character in name
        if character.isprintable() and character not in {'"', "\r", "\n", "\x00"}
    )
    if name in {"", ".", ".."}:
        name = "attachment"
    return name[:180]


def _extension(filename: str) -> str:
    suffix = Path(filename).suffix.lower()
    return suffix if suffix in ALLOWED_EXTENSIONS else ".bin"


def storage_root(settings: Settings) -> Path:
    root = Path(settings.attachment_root)
    root.mkdir(parents=True, exist_ok=True)
    return root.resolve()


def resolve_object_path(settings: Settings, object_key: str) -> Path:
    root = storage_root(settings)
    target = (root / object_key).resolve()
    if not target.is_relative_to(root):
        raise AttachmentPathError("attachment path escapes the storage root")
    return target


async def save_attachment(
    session: AsyncSession,
    settings: Settings,
    employee: Employee,
    ticket_id: uuid.UUID,
    filename: str | None,
    content_type: str,
    data: bytes,
) -> dict:
    ticket = await get_ticket_for_employee(session, employee, ticket_id, "read")
    if not data:
        raise AttachmentError("attachment is empty")
    if len(data) > settings.attachment_max_bytes:
        raise AttachmentTooLarge(
            f"attachment exceeds {settings.attachment_max_bytes} bytes"
        )

    safe_name = sanitize_filename(filename)
    object_key = (
        f"{ticket.tenant_id}/{ticket.store_id}/{ticket.id}/"
        f"{uuid.uuid4().hex}{_extension(safe_name)}"
    )
    target = resolve_object_path(settings, object_key)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_suffix(target.suffix + ".part")
    temp.write_bytes(data)
    os.replace(temp, target)

    digest = hashlib.sha256(data).hexdigest()
    attachment = Attachment(
        tenant_id=ticket.tenant_id,
        store_id=ticket.store_id,
        ticket_id=ticket.id,
        object_key=object_key,
        filename=safe_name,
        content_type=content_type or "application/octet-stream",
        size=len(data),
        sha256=digest,
        access_scope="ticket",
    )
    session.add(attachment)
    await session.commit()
    return {
        "id": str(attachment.id),
        "ticket_id": str(ticket.id),
        "filename": attachment.filename,
        "content_type": attachment.content_type,
        "size": attachment.size,
        "sha256": attachment.sha256,
        "access_scope": attachment.access_scope,
        "object_key": attachment.object_key,
    }


async def load_attachment(
    session: AsyncSession,
    settings: Settings,
    employee: Employee,
    ticket_id: uuid.UUID,
    attachment_id: uuid.UUID,
) -> tuple[Attachment, Path]:
    attachment = await session.get(Attachment, attachment_id)
    if attachment is None or attachment.ticket_id != ticket_id:
        raise HTTPException(status_code=404, detail="attachment not found")
    await get_ticket_for_employee(session, employee, ticket_id, "read")
    path = resolve_object_path(settings, attachment.object_key)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="attachment file missing")
    return attachment, path


async def ocr_attachment(
    session: AsyncSession,
    settings: Settings,
    adapter: OcrAdapter,
    employee: Employee,
    ticket_id: uuid.UUID,
    attachment_id: uuid.UUID,
) -> dict:
    attachment, path = await load_attachment(
        session, settings, employee, ticket_id, attachment_id
    )
    try:
        result = await adapter.extract(
            path.read_bytes(), attachment.content_type, attachment.filename
        )
    except NotConfigured as exc:
        raise HTTPException(status_code=503, detail=f"ocr not configured: {exc}") from exc
    return {
        "attachment_id": str(attachment.id),
        "ticket_id": str(attachment.ticket_id),
        "filename": attachment.filename,
        "provider": result.provider,
        "text": result.text,
        "chars": len(result.text),
    }