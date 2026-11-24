from __future__ import annotations

import uuid
from dataclasses import dataclass

from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    ChannelAccount,
    Conversation,
    Message,
    MessageDirection,
    Store,
    Tenant,
    Ticket,
    TicketEvent,
    TicketStatus,
    TranslationState,
)
from app.schemas import InboundPayload
from app.security import verify_hmac
from app.trace import Timer, log_event, new_trace_id, utcnow


@dataclass
class InboundResult:
    ticket_id: uuid.UUID
    ticket_number: str
    duplicate: bool
    trace_id: str


async def authenticate_channel(
    session: AsyncSession,
    account_code: str,
    raw_body: bytes,
    signature: str,
) -> ChannelAccount:
    account = (
        await session.execute(
            select(ChannelAccount).where(
                ChannelAccount.code == account_code,
                ChannelAccount.enabled.is_(True),
                ChannelAccount.verified.is_(True),
            )
        )
    ).scalar_one_or_none()
    if account is None or not verify_hmac(account.webhook_secret, raw_body, signature):
        # Deliberately identical for unknown account and bad signature.
        raise HTTPException(status_code=401, detail="invalid channel authentication")
    return account


async def ingest_inbound(
    session: AsyncSession,
    account: ChannelAccount,
    payload: InboundPayload,
) -> InboundResult:
    timer = Timer()
    trace_id = new_trace_id()
    try:
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
            {"key": f"{account.id}:{payload.external_message_id}"},
        )

        existing = (
            await session.execute(
                select(Message).where(
                    Message.channel_account_id == account.id,
                    Message.external_message_id == payload.external_message_id,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            ticket: Ticket | None = None
            if existing.ticket_id is not None:
                ticket = await session.get(Ticket, existing.ticket_id)
            if ticket is None:
                ticket = (
                    await session.execute(
                        select(Ticket).where(
                            Ticket.conversation_id == existing.conversation_id,
                            Ticket.status != TicketStatus.closed,
                        )
                    )
                ).scalar_one_or_none()
            if ticket is None:
                raise HTTPException(status_code=409, detail="duplicate message has no ticket")
            await session.commit()
            log_event(
                "inbound_duplicate",
                trace_id=existing.trace_id,
                ticket_id=str(ticket.id),
                channel_account=account.code,
                duration_ms=timer.elapsed_ms,
            )
            return InboundResult(ticket.id, ticket.number, True, existing.trace_id)

        tenant = await session.get(Tenant, account.tenant_id)
        store = await session.get(Store, account.store_id)
        if tenant is None or store is None:
            raise HTTPException(status_code=500, detail="channel account scope is invalid")

        await session.execute(
            pg_insert(Conversation)
            .values(
                id=uuid.uuid4(),
                tenant_id=account.tenant_id,
                store_id=account.store_id,
                channel_account_id=account.id,
                buyer_external_id=payload.buyer_external_id,
                buyer_display_name=payload.buyer_display_name,
            )
            .on_conflict_do_nothing(
                index_elements=["channel_account_id", "buyer_external_id"]
            )
        )
        conversation = (
            await session.execute(
                select(Conversation)
                .where(
                    Conversation.channel_account_id == account.id,
                    Conversation.buyer_external_id == payload.buyer_external_id,
                )
                .with_for_update()
            )
        ).scalar_one()

        ticket = (
            await session.execute(
                select(Ticket).where(
                    Ticket.conversation_id == conversation.id,
                    Ticket.status != TicketStatus.closed,
                )
            )
        ).scalar_one_or_none()
        if ticket is None:
            ticket = Ticket(
                tenant_id=account.tenant_id,
                store_id=account.store_id,
                conversation_id=conversation.id,
                number=(
                    f"{tenant.code}-{utcnow():%Y%m%d}-"
                    f"{uuid.uuid4().hex[:6].upper()}"
                ),
                status=TicketStatus.open,
                intents=[],
                risk_flags=[],
                order_candidates=list(payload.order_candidates),
            )
            session.add(ticket)
            await session.flush()
            session.add(
                TicketEvent(
                    tenant_id=account.tenant_id,
                    ticket_id=ticket.id,
                    kind="ticket_created",
                    trace_id=trace_id,
                    payload={"source": "inbound", "channel_account": account.code},
                )
            )
        else:
            candidates = list(ticket.order_candidates or [])
            for candidate in payload.order_candidates:
                if candidate not in candidates:
                    candidates.append(candidate)
            ticket.order_candidates = candidates
            ticket.updated_at = utcnow()

        message = Message(
            tenant_id=account.tenant_id,
            store_id=account.store_id,
            conversation_id=conversation.id,
            ticket_id=ticket.id,
            channel_account_id=account.id,
            external_message_id=payload.external_message_id,
            direction=MessageDirection.inbound,
            body_text=payload.text,
            original_language="ja",
            translation_state=TranslationState.pending,
            buyer_external_id=payload.buyer_external_id,
            raw_payload=payload.model_dump(mode="json"),
            trace_id=trace_id,
        )
        session.add(message)
        await session.flush()

        session.add(
            TicketEvent(
                tenant_id=account.tenant_id,
                ticket_id=ticket.id,
                kind="inbound_received",
                trace_id=trace_id,
                payload={
                    "message_id": str(message.id),
                    "external_message_id": payload.external_message_id,
                    "text_length": len(payload.text),
                    "order_candidates": payload.order_candidates,
                },
            )
        )
        await session.commit()
        log_event(
            "inbound_created",
            trace_id=trace_id,
            ticket_id=str(ticket.id),
            channel_account=account.code,
            tenant_id=str(account.tenant_id),
            store_id=str(account.store_id),
            duration_ms=timer.elapsed_ms,
        )
        return InboundResult(ticket.id, ticket.number, False, trace_id)
    except HTTPException:
        await session.rollback()
        raise
    except Exception:
        await session.rollback()
        log_event(
            "inbound_failed",
            trace_id=trace_id,
            channel_account=account.code,
            duration_ms=timer.elapsed_ms,
        )
        raise