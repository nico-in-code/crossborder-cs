from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Protocol

import httpx

from app.adapters.integrations import (
    NotConfigured,
    PermissionDenied,
    UpstreamError,
    UpstreamTimeout,
)
from app.config import Settings


@dataclass(frozen=True)
class OcrResult:
    text: str
    provider: str


class OcrAdapter(Protocol):
    async def extract(self, data: bytes, content_type: str, filename: str) -> OcrResult: ...


class FakeOcrAdapter:
    """Deterministic local OCR so attachment flows are testable without a model."""

    provider = "fake"

    async def extract(self, data: bytes, content_type: str, filename: str) -> OcrResult:
        kind = "PDF" if content_type == "application/pdf" else "图片"
        digest = hashlib.sha256(data).hexdigest()[:12]
        return OcrResult(
            text=f"[本地 OCR 模拟] {kind}附件 {filename}，大小 {len(data)} 字节，sha256:{digest}",
            provider=self.provider,
        )


class HttpOcrAdapter:
    """On-demand OCR gateway; no result is persisted unless the caller stores it."""

    def __init__(
        self, settings: Settings, *, client: httpx.AsyncClient | None = None
    ):
        self.settings = settings
        self.client = client or httpx.AsyncClient(timeout=30.0)

    async def extract(self, data: bytes, content_type: str, filename: str) -> OcrResult:
        if not self.settings.ocr_base_url:
            raise NotConfigured("OCR_BASE_URL is not configured")
        headers = {"Accept": "application/json"}
        if self.settings.ocr_api_key:
            headers["X-API-Key"] = self.settings.ocr_api_key
        try:
            response = await self.client.post(
                f"{self.settings.ocr_base_url.rstrip('/')}/ocr",
                headers=headers,
                files={"file": (filename, data, content_type)},
            )
        except httpx.TimeoutException as exc:
            raise UpstreamTimeout("OCR gateway timed out") from exc
        if response.status_code in {401, 403}:
            raise PermissionDenied(f"OCR gateway denied access ({response.status_code})")
        if response.status_code >= 500:
            raise UpstreamError(f"OCR gateway returned {response.status_code}")
        if response.status_code >= 400:
            raise UpstreamError(f"OCR gateway returned {response.status_code}")
        body = response.json() if response.content else {}
        text = body.get("text")
        if text is None:
            raise UpstreamError("OCR gateway returned no text")
        return OcrResult(text=str(text), provider="http")


def build_ocr_adapter(settings: Settings) -> OcrAdapter:
    if settings.ocr_provider == "http":
        return HttpOcrAdapter(settings)
    return FakeOcrAdapter()