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


class HttpTranslator:
    """Controlled translation gateway; calls are scoped to an enabled store."""

    def __init__(
        self, settings: Settings, *, client: httpx.AsyncClient | None = None
    ):
        self.settings = settings
        self.client = client or httpx.AsyncClient(timeout=10.0)
        self.enablement = StoreEnablement.from_settings(
            settings.integration_enabled_store_ids
        )

    def _ensure(self, store_id: uuid.UUID | None) -> None:
        if not self.settings.translation_enabled:
            raise NotConfigured("translation integration is disabled")
        if not self.settings.translation_base_url:
            raise NotConfigured("TRANSLATION_BASE_URL is not configured")
        if store_id is None:
            if self.enablement.store_ids:
                raise NotConfigured("translation calls require an enabled store scope")
            return
        ensure_ready(
            name="translation",
            flag=True,
            credentials={"TRANSLATION_BASE_URL": self.settings.translation_base_url},
            store_ids={store_id},
            enablement=self.enablement,
        )

    async def translate(
        self,
        text: str,
        source_language: str,
        target_language: str,
        store_id: uuid.UUID | None = None,
    ) -> str:
        self._ensure(store_id)
        headers = {"Accept": "application/json"}
        if self.settings.translation_api_key:
            headers["X-API-Key"] = self.settings.translation_api_key
        try:
            response = await self.client.post(
                f"{self.settings.translation_base_url.rstrip('/')}/translate",
                headers=headers,
                json={
                    "text": text,
                    "source_language": source_language,
                    "target_language": target_language,
                },
            )
        except httpx.TimeoutException as exc:
            raise UpstreamTimeout("translation gateway timed out") from exc
        if response.status_code in {401, 403}:
            raise PermissionDenied(
                f"translation gateway denied access ({response.status_code})"
            )
        if response.status_code >= 500:
            raise UpstreamError(f"translation gateway returned {response.status_code}")
        if response.status_code >= 400:
            raise UpstreamError(f"translation gateway returned {response.status_code}")
        body = response.json() if response.content else {}
        translated = body.get("translated_text") or body.get("text")
        if not translated:
            raise UpstreamError("translation gateway returned no translated_text")
        return str(translated)