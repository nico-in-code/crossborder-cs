from __future__ import annotations

import uuid

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


class ErpInventoryAdapter:
    """Read-only inventory lookup against the operator's ERP gateway."""

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
            name="erp",
            flag=self.settings.erp_enabled,
            credentials={
                "ERP_BASE_URL": self.settings.erp_base_url,
                "ERP_API_KEY": self.settings.erp_api_key,
            },
            store_ids={store_id},
            enablement=self.enablement,
        )

    async def get_inventory(self, store_id: uuid.UUID, sku: str) -> dict | None:
        self._ensure(store_id)
        url = f"{self.settings.erp_base_url.rstrip('/')}/inventory/{sku}"
        try:
            response = await self.client.get(
                url,
                params={"store_id": str(store_id)},
                headers={
                    "X-API-Key": self.settings.erp_api_key,
                    "Accept": "application/json",
                },
            )
        except httpx.TimeoutException as exc:
            raise UpstreamTimeout("ERP inventory request timed out") from exc
        if response.status_code == 404:
            return None
        if response.status_code in {401, 403}:
            raise PermissionDenied(f"ERP denied access ({response.status_code})")
        if response.status_code >= 500:
            raise UpstreamError(f"ERP returned {response.status_code}")
        if response.status_code >= 400:
            raise UpstreamError(f"ERP returned {response.status_code}")
        body = response.json() if response.content else {}
        if "quantity" not in body:
            raise UpstreamError("ERP response did not contain a quantity")
        return {
            "sku": body.get("sku") or sku,
            "quantity": int(body["quantity"]),
            "updated_at": body.get("updated_at"),
        }


__all__ = ["ErpInventoryAdapter", "NotConfigured"]