from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import get_db
from app.schemas import InboundPayload
from app.services.inbound import authenticate_channel, ingest_inbound

router = APIRouter(tags=["inbound"])


@router.post("/api/inbound/{account_code}")
async def receive_inbound(
    account_code: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    raw_body = await request.body()
    signature = request.headers.get("X-Channel-Signature", "")
    account = await authenticate_channel(db, account_code, raw_body, signature)
    try:
        payload = InboundPayload.model_validate_json(raw_body)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="invalid inbound payload") from exc
    result = await ingest_inbound(db, account, payload)
    return {
        "ticket_id": str(result.ticket_id),
        "ticket_number": result.ticket_number,
        "duplicate": result.duplicate,
        "trace_id": result.trace_id,
        "ticket_url": f"/tickets/{result.ticket_id}",
    }