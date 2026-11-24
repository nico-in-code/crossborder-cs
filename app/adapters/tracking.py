from __future__ import annotations

import uuid
from datetime import datetime, timezone

import httpx

from app.adapters.integrations import (
    NotConfigured,
    PermissionDenied,
    StoreEnablement,
    UpstreamError,
    UpstreamTimeout,
    ensure_ready,
)
from app.config import Settings


class Track17Adapter:
    """17TRACK v2.2 contract client; each tracking read is store-scoped."""

    def __init__(
        self, settings: Settings, *, client: httpx.AsyncClient | None = None
    ):
        self.settings = settings
        self.client = client or httpx.AsyncClient(timeout=10.0)
        self.enablement = StoreEnablement.from_settings(
            settings.integration_enabled_store_ids
        )

    def _ensure(self, store_id: uuid.UUID) -> None:
        ensure_ready(
            name="track17",
            flag=self.settings.track17_enabled,
            credentials={"TRACK17_API_KEY": self.settings.track17_api_key},
            store_ids={store_id},
            enablement=self.enablement,
        )
        if not self.settings.track17_base_url:
            raise NotConfigured("TRACK17_BASE_URL is not configured")

    def _url(self, action: str) -> str:
        return f"{self.settings.track17_base_url.rstrip('/')}/{action}"

    async def _post(self, action: str, payload: dict) -> dict:
        try:
            response = await self.client.post(
                self._url(action),
                headers={
                    "17token": self.settings.track17_api_key,
                    "Accept": "application/json",
                },
                json=payload,
            )
        except httpx.TimeoutException as exc:
            raise UpstreamTimeout("17TRACK request timed out") from exc
        if response.status_code in {401, 403}:
            raise PermissionDenied(f"17TRACK denied access ({response.status_code})")
        if response.status_code >= 500:
            raise UpstreamError(f"17TRACK returned {response.status_code}")
        if response.status_code >= 400:
            raise UpstreamError(f"17TRACK returned {response.status_code}")
        return response.json() if response.content else {}

    async def track(
        self, store_id: uuid.UUID, tracking_no: str, carrier: str | None = None
    ) -> dict:
        self._ensure(store_id)
        item = {"number": tracking_no}
        if carrier:
            item["carrier"] = carrier
        await self._post("register", {"data": [item]})
        payload = await self._post("gettrackinfo", {"data": [item]})
        if payload.get("code") not in (None, 0):
            raise UpstreamError(f"17TRACK error code {payload.get('code')}")
        accepted = ((payload.get("data") or {}).get("accepted") or [])
        if not accepted:
            return {
                "tracking_no": tracking_no,
                "carrier": carrier,
                "status": "not_found",
                "events": [],
                "synced_at": datetime.now(timezone.utc).isoformat(),
            }
        record = accepted[0]
        info = record.get("track_info") or {}
        providers = (info.get("tracking") or {}).get("providers") or []
        events = []
        for provider in providers:
            for event in provider.get("events") or []:
                events.append(
                    {
                        "at": event.get("time_iso") or event.get("time_utc"),
                        "status": event.get("stage"),
                        "description": event.get("description"),
                    }
                )
        latest = info.get("latest_status") or {}
        return {
            "tracking_no": record.get("number") or tracking_no,
            "carrier": record.get("carrier") or carrier,
            "status": latest.get("status") or "unknown",
            "events": events,
            "synced_at": datetime.now(timezone.utc).isoformat(),
        }