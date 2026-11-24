from __future__ import annotations

from typing import Protocol


class TranslatorNotConfigured(RuntimeError):
    pass


class Translator(Protocol):
    async def translate(self, text: str, source_language: str, target_language: str) -> str: ...


class DeterministicTranslator:
    """Keyword-based stand-in so local demos are repeatable without a model."""

    async def translate(
        self, text: str, source_language: str, target_language: str
    ) -> str:
        if target_language != "zh":
            return text
        if "届いていません" in text and "返金" in text:
            return "物流显示已送达但买家未收到包裹，并申请退款。"
        if "届いていません" in text:
            return "物流显示已送达但买家表示未收到包裹。"
        if "返金" in text:
            return "买家申请退款。"
        return f"[{source_language}->zh] {text}"


class DeepLTranslator:
    def __init__(self, api_key: str):
        self.api_key = api_key

    async def translate(
        self, text: str, source_language: str, target_language: str
    ) -> str:
        if not self.api_key:
            raise TranslatorNotConfigured("DEEPL_API_KEY is not configured")
        raise TranslatorNotConfigured(
            "DeepL live translation is not enabled in this local delivery"
        )