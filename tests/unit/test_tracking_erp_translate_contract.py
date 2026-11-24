from __future__ import annotations

import json
import uuid

import httpx
import pytest

from app.adapters.erp import ErpInventoryAdapter
from app.adapters.integrations import (
    NotConfigured,
    PermissionDenied,
    UpstreamError,
    UpstreamTimeout,
)
from app.adapters.tracking import Track17Adapter
from app.adapters.translate_http import HttpTranslator
from app.config import Settings

STORE = uuid.uuid4()
OTHER_STORE = uuid.uuid4()


def build_client(handler):
    requests: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handler(request)

    return httpx.AsyncClient(transport=httpx.MockTransport(wrapped)), requests


def track17_settings(**overrides) -> Settings:
    values = {
        "track17_enabled": True,
        "track17_api_key": "track-key",
        "track17_base_url": "https://api.17track.net/track/v2.2",
        "integration_enabled_store_ids": str(STORE),
    }
    values.update(overrides)
    return Settings(**values)


def erp_settings(**overrides) -> Settings:
    values = {
        "erp_enabled": True,
        "erp_base_url": "https://erp.example",
        "erp_api_key": "erp-key",
        "integration_enabled_store_ids": str(STORE),
    }
    values.update(overrides)
    return Settings(**values)


def translation_settings(**overrides) -> Settings:
    values = {
        "translation_enabled": True,
        "translation_base_url": "https://translate.example",
        "translation_api_key": "translate-key",
        "integration_enabled_store_ids": str(STORE),
    }
    values.update(overrides)
    return Settings(**values)


TRACK_RESPONSE = {
    "code": 0,
    "data": {
        "accepted": [
            {
                "number": "YMT-A-0001",
                "carrier": 1003,
                "track_info": {
                    "tracking": {
                        "providers": [
                            {
                                "events": [
                                    {
                                        "time_iso": "2026-11-21T08:00:00Z",
                                        "stage": "delivered",
                                        "description": "配達完了",
                                    }
                                ]
                            }
                        ]
                    },
                    "latest_status": {"status": "Delivered"},
                },
            }
        ]
    },
}


@pytest.mark.unit
async def test_track17_success_normalizes_events():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path in {"/track/v2.2/register", "/track/v2.2/gettrackinfo"}
        assert request.headers["17token"] == "track-key"
        body = json.loads(request.content)
        assert body["data"][0]["number"] == "YMT-A-0001"
        return httpx.Response(200, json=TRACK_RESPONSE)

    client, requests = build_client(handler)
    adapter = Track17Adapter(track17_settings(), client=client)
    result = await adapter.track(STORE, "YMT-A-0001", "yamato")
    assert len(requests) == 2
    assert result["status"] == "Delivered"
    assert result["events"][0]["description"] == "配達完了"


@pytest.mark.unit
async def test_track17_error_mapping_and_not_configured():
    def forbidden(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403)

    client, _ = build_client(forbidden)
    adapter = Track17Adapter(track17_settings(), client=client)
    with pytest.raises(PermissionDenied):
        await adapter.track(STORE, "YMT-A-0001")

    client, _ = build_client(lambda request: httpx.Response(500))
    adapter = Track17Adapter(track17_settings(), client=client)
    with pytest.raises(UpstreamError):
        await adapter.track(STORE, "YMT-A-0001")

    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    client, _ = build_client(timeout)
    adapter = Track17Adapter(track17_settings(), client=client)
    with pytest.raises(UpstreamTimeout):
        await adapter.track(STORE, "YMT-A-0001")

    adapter = Track17Adapter(track17_settings(track17_enabled=False))
    with pytest.raises(NotConfigured):
        await adapter.track(STORE, "YMT-A-0001")

    adapter = Track17Adapter(track17_settings(track17_api_key=""))
    with pytest.raises(NotConfigured):
        await adapter.track(STORE, "YMT-A-0001")

    adapter = Track17Adapter(track17_settings())
    with pytest.raises(NotConfigured):
        await adapter.track(OTHER_STORE, "YMT-A-0001")


@pytest.mark.unit
async def test_erp_inventory_success_and_errors():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/inventory/SKU-JP-001"
        assert str(request.url.params["store_id"]) == str(STORE)
        assert request.headers["x-api-key"] == "erp-key"
        return httpx.Response(
            200,
            json={"sku": "SKU-JP-001", "quantity": 42, "updated_at": "2026-11-20T00:00:00Z"},
        )

    client, _ = build_client(handler)
    adapter = ErpInventoryAdapter(erp_settings(), client=client)
    inventory = await adapter.get_inventory(STORE, "SKU-JP-001")
    assert inventory == {
        "sku": "SKU-JP-001",
        "quantity": 42,
        "updated_at": "2026-11-20T00:00:00Z",
    }

    client, _ = build_client(lambda request: httpx.Response(404))
    adapter = ErpInventoryAdapter(erp_settings(), client=client)
    assert await adapter.get_inventory(STORE, "SKU-JP-001") is None

    client, _ = build_client(lambda request: httpx.Response(401))
    adapter = ErpInventoryAdapter(erp_settings(), client=client)
    with pytest.raises(PermissionDenied):
        await adapter.get_inventory(STORE, "SKU-JP-001")

    client, _ = build_client(lambda request: httpx.Response(502))
    adapter = ErpInventoryAdapter(erp_settings(), client=client)
    with pytest.raises(UpstreamError):
        await adapter.get_inventory(STORE, "SKU-JP-001")

    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("slow", request=request)

    client, _ = build_client(timeout)
    adapter = ErpInventoryAdapter(erp_settings(), client=client)
    with pytest.raises(UpstreamTimeout):
        await adapter.get_inventory(STORE, "SKU-JP-001")

    adapter = ErpInventoryAdapter(erp_settings(erp_enabled=False))
    with pytest.raises(NotConfigured):
        await adapter.get_inventory(STORE, "SKU-JP-001")


@pytest.mark.unit
async def test_translation_gateway_contract():
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body == {
            "text": "届いていません",
            "source_language": "ja",
            "target_language": "zh",
        }
        assert request.headers["x-api-key"] == "translate-key"
        return httpx.Response(200, json={"translated_text": "未收到包裹"})

    client, _ = build_client(handler)
    translator = HttpTranslator(translation_settings(), client=client)
    assert (
        await translator.translate("届いていません", "ja", "zh", store_id=STORE)
        == "未收到包裹"
    )

    client, _ = build_client(lambda request: httpx.Response(403))
    translator = HttpTranslator(translation_settings(), client=client)
    with pytest.raises(PermissionDenied):
        await translator.translate("text", "ja", "zh", store_id=STORE)

    client, _ = build_client(lambda request: httpx.Response(500))
    translator = HttpTranslator(translation_settings(), client=client)
    with pytest.raises(UpstreamError):
        await translator.translate("text", "ja", "zh", store_id=STORE)

    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    client, _ = build_client(timeout)
    translator = HttpTranslator(translation_settings(), client=client)
    with pytest.raises(UpstreamTimeout):
        await translator.translate("text", "ja", "zh", store_id=STORE)

    translator = HttpTranslator(translation_settings(translation_enabled=False))
    with pytest.raises(NotConfigured):
        await translator.translate("text", "ja", "zh", store_id=STORE)

    translator = HttpTranslator(translation_settings())
    with pytest.raises(NotConfigured):
        await translator.translate("text", "ja", "zh")

    translator = HttpTranslator(translation_settings())
    with pytest.raises(NotConfigured):
        await translator.translate("text", "ja", "zh", store_id=OTHER_STORE)