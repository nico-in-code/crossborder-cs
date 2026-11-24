from __future__ import annotations

import uuid
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import ChannelReceipt


class ChannelTimeout(Exception):
    """The channel accepted the request but the response did not arrive."""


class ChannelRejected(Exception):
    """The channel explicitly rejected the message."""


class ChannelAdapter(Protocol):
    async def send(
        self,
        *,
        tenant_id: uuid.UUID,
        channel_account_id: uuid.UUID,
        idempotency_key: str,
        body_hash: str,
        payload: dict,
    ) -> ChannelReceipt: ...

    async def lookup_receipt(
        self, channel_account_id: uuid.UUID, idempotency_key: str
    ) -> ChannelReceipt | None: ...


class FakeChannelAdapter:
    """Local channel double that persists a receipt before optionally timing out."""

    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession], mode: str = "normal"):
        self.sessionmaker = sessionmaker
        self.mode = mode
        self.send_calls = 0

    async def send(
        self,
        *,
        tenant_id: uuid.UUID,
        channel_account_id: uuid.UUID,
        idempotency_key: str,
        body_hash: str,
        payload: dict,
    ) -> ChannelReceipt:
        self.send_calls += 1
        if self.mode == "reject":
            raise ChannelRejected(idempotency_key)
        async with self.sessionmaker() as session:
            existing = await session.scalar(
                select(ChannelReceipt).where(
                    ChannelReceipt.channel_account_id == channel_account_id,
                    ChannelReceipt.idempotency_key == idempotency_key,
                )
            )
            if existing is not None:
                return existing
            receipt = ChannelReceipt(
                tenant_id=tenant_id,
                channel_account_id=channel_account_id,
                idempotency_key=idempotency_key,
                external_send_id=f"ext-{uuid.uuid4().hex[:20]}",
                body_hash=body_hash,
                payload=payload,
            )
            session.add(receipt)
            await session.commit()
            await session.refresh(receipt)

        if self.mode == "accept_then_timeout":
            raise ChannelTimeout(idempotency_key)
        return receipt

    async def lookup_receipt(
        self, channel_account_id: uuid.UUID, idempotency_key: str
    ) -> ChannelReceipt | None:
        async with self.sessionmaker() as session:
            return await session.scalar(
                select(ChannelReceipt).where(
                    ChannelReceipt.channel_account_id == channel_account_id,
                    ChannelReceipt.idempotency_key == idempotency_key,
                )
            )