from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Awaitable, Callable

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.adapters.channel import ChannelRejected, ChannelTimeout
from app.adapters.integrations import (
    NotConfigured,
    StoreEnablement,
    UpstreamError,
    UpstreamTimeout,
    ensure_ready,
)
from app.config import Settings
from app.models import ChannelAccount

StoreResolver = Callable[[uuid.UUID], Awaitable[uuid.UUID | None]]


@dataclass(frozen=True)
class ChannelReceiptView:
    external_send_id: str
    status: str = "accepted"


def make_store_resolver(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> StoreResolver:
    async def resolve(channel_account_id: uuid.UUID) -> uuid.UUID | None:
        async with sessionmaker() as session:
            return await session.scalar(
                select(ChannelAccount.store_id).where(
                    ChannelAccount.id == channel_account_id
                )
            )

    return resolve


class HttpChannelAdapter:
    """Pilot channel contract client; disabled unless a store is enabled.

    The upstream must honour ``Idempotency-Key`` and expose
    ``GET /messages/{idempotency_key}`` so a timeout can be reconciled without
    sending twice.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        store_resolver: StoreResolver,
        client: httpx.AsyncClient | None = None,
    ):
        self.settings = settings
        self.store_resolver = store_resolver
        self.client = client or httpx.AsyncClient(
            timeout=settings.channel_timeout_seconds
        )
        self.enablement = StoreEnablement.from_settings(
            settings.integration_enabled_store_ids
        )

    async def _ensure(self, channel_account_id: uuid.UUID) -> uuid.UUID:
        store_id = await self.store_resolver(channel_account_id)
        if store_id is None:
            raise NotConfigured("channel account does not resolve to a store")
        return ensure_ready(
            name="channel",
            flag=self.settings.channel_enabled,
            credentials={
                "CHANNEL_BASE_URL": self.settings.channel_base_url,
                "CHANNEL_API_KEY": self.settings.channel_api_key,
            },
            store_ids={store_id},
            enablement=self.enablement,
        )

    def _url(self, path: str) -> str:
        return f"{self.settings.channel_base_url.rstrip('/')}{path}"

    async def send(
        self,
        *,
        tenant_id: uuid.UUID,
        channel_account_id: uuid.UUID,
        idempotency_key: str,
        body_hash: str,
        payload: dict,
    ) -> ChannelReceiptView:
        await self._ensure(channel_account_id)
        try:
            response = await self.client.post(
                self._url("/messages"),
                headers={
                    "X-API-Key": self.settings.channel_api_key,
                    "Idempotency-Key": idempotency_key,
                    "Accept": "application/json",
                },
                json={
                    "tenant_id": str(tenant_id),
                    "channel_account_id": str(channel_account_id),
                    "idempotency_key": idempotency_key,
                    "body_hash": body_hash,
                    "message": payload,
                },
            )
        except httpx.TimeoutException as exc:
            raise ChannelTimeout(idempotency_key) from exc
        if response.status_code in {401, 403}:
            raise ChannelRejected(f"pilot_channel_denied_{response.status_code}")
        if response.status_code >= 500:
            raise UpstreamError(f"pilot channel returned {response.status_code}")
        if response.status_code >= 400:
            raise ChannelRejected(f"pilot_channel_rejected_{response.status_code}")
        body = response.json() if response.content else {}
        external_id = body.get("message_id") or body.get("id") or idempotency_key
        return ChannelReceiptView(external_send_id=str(external_id), status="accepted")

    async def lookup_receipt(
        self, channel_account_id: uuid.UUID, idempotency_key: str
    ) -> ChannelReceiptView | None:
        await self._ensure(channel_account_id)
        try:
            response = await self.client.get(
                self._url(f"/messages/{idempotency_key}"),
                headers={
                    "X-API-Key": self.settings.channel_api_key,
                    "Accept": "application/json",
                },
            )
        except httpx.TimeoutException as exc:
            raise ChannelTimeout(idempotency_key) from exc
        if response.status_code == 404:
            return None
        if response.status_code in {401, 403}:
            raise ChannelRejected(f"pilot_channel_denied_{response.status_code}")
        if response.status_code >= 500:
            raise UpstreamError(f"pilot channel returned {response.status_code}")
        if response.status_code >= 400:
            raise UpstreamError(f"pilot channel returned {response.status_code}")
        body = response.json() if response.content else {}
        external_id = body.get("message_id") or body.get("id")
        if not external_id:
            return None
        return ChannelReceiptView(
            external_send_id=str(external_id),
            status=body.get("status") or "accepted",
        )