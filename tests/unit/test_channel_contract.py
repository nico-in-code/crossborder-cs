from __future__ import annotations

import json
import uuid

import httpx
import pytest

from app.adapters.channel import ChannelRejected, ChannelTimeout
from app.adapters.channel_http import HttpChannelAdapter
from app.adapters.integrations import NotConfigured, UpstreamError
from app.config import Settings

STORE = uuid.uuid4()
OTHER_STORE = uuid.uuid4()
ACCOUNT = uuid.uuid4()
TENANT = uuid.uuid4()


def make_settings(**overrides) -> Settings:
    values = {
        "channel_enabled": True,
        "channel_base_url": "https://pilot-channel.example",
        "channel_api_key": "pilot-key",
        "channel_timeout_seconds": 5.0,
        "integration_enabled_store_ids": str(STORE),
    }
    values.update(overrides)
    return Settings(**values)


def make_resolver(store_id: uuid.UUID | None = STORE):
    async def resolve(channel_account_id: uuid.UUID) -> uuid.UUID | None:
        assert channel_account_id == ACCOUNT
        return store_id

    return resolve


def build(handler, *, store_id: uuid.UUID | None = STORE, **overrides):
    requests: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handler(request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(wrapped))
    adapter = HttpChannelAdapter(
        make_settings(**overrides),
        store_resolver=make_resolver(store_id),
        client=client,
    )
    return adapter, requests


def success_handler(request: httpx.Request) -> httpx.Response:
    if request.method == "POST":
        return httpx.Response(201, json={"message_id": "msg-1", "status": "accepted"})
    return httpx.Response(200, json={"message_id": "msg-1", "status": "delivered"})


@pytest.mark.unit
async def test_send_declares_idempotency_and_returns_receipt():
    adapter, requests = build(success_handler)
    receipt = await adapter.send(
        tenant_id=TENANT,
        channel_account_id=ACCOUNT,
        idempotency_key="ticket-1:draft-1",
        body_hash="hash-1",
        payload={"body": "こんにちは", "ticket_id": str(uuid.uuid4())},
    )
    assert receipt.external_send_id == "msg-1"
    request = requests[0]
    assert request.method == "POST"
    assert request.url.path == "/messages"
    assert request.headers["idempotency-key"] == "ticket-1:draft-1"
    assert request.headers["x-api-key"] == "pilot-key"
    body = json.loads(request.content)
    assert body["idempotency_key"] == "ticket-1:draft-1"
    assert body["body_hash"] == "hash-1"
    assert body["message"]["body"] == "こんにちは"


@pytest.mark.unit
async def test_lookup_receipt_success_and_missing():
    adapter, requests = build(success_handler)
    receipt = await adapter.lookup_receipt(ACCOUNT, "ticket-1:draft-1")
    assert receipt is not None and receipt.external_send_id == "msg-1"
    assert requests[0].method == "GET"
    assert requests[0].url.path == "/messages/ticket-1:draft-1"

    adapter, _ = build(lambda request: httpx.Response(404))
    assert await adapter.lookup_receipt(ACCOUNT, "missing") is None


@pytest.mark.unit
async def test_403_maps_to_channel_rejected():
    adapter, _ = build(lambda request: httpx.Response(403))
    with pytest.raises(ChannelRejected):
        await adapter.send(
            tenant_id=TENANT,
            channel_account_id=ACCOUNT,
            idempotency_key="k",
            body_hash="h",
            payload={},
        )


@pytest.mark.unit
async def test_4xx_maps_to_channel_rejected():
    adapter, _ = build(lambda request: httpx.Response(422, json={"error": "bad"}))
    with pytest.raises(ChannelRejected):
        await adapter.send(
            tenant_id=TENANT,
            channel_account_id=ACCOUNT,
            idempotency_key="k",
            body_hash="h",
            payload={},
        )


@pytest.mark.unit
async def test_500_maps_to_upstream_error():
    adapter, _ = build(lambda request: httpx.Response(500, text="boom"))
    with pytest.raises(UpstreamError):
        await adapter.send(
            tenant_id=TENANT,
            channel_account_id=ACCOUNT,
            idempotency_key="k",
            body_hash="h",
            payload={},
        )
    with pytest.raises(UpstreamError):
        await adapter.lookup_receipt(ACCOUNT, "k")


@pytest.mark.unit
async def test_timeout_maps_to_channel_timeout():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("no response", request=request)

    adapter, _ = build(handler)
    with pytest.raises(ChannelTimeout):
        await adapter.send(
            tenant_id=TENANT,
            channel_account_id=ACCOUNT,
            idempotency_key="k",
            body_hash="h",
            payload={},
        )
    with pytest.raises(ChannelTimeout):
        await adapter.lookup_receipt(ACCOUNT, "k")


@pytest.mark.unit
async def test_disabled_or_unscoped_store_is_not_configured():
    adapter, _ = build(success_handler, channel_enabled=False)
    with pytest.raises(NotConfigured):
        await adapter.send(
            tenant_id=TENANT,
            channel_account_id=ACCOUNT,
            idempotency_key="k",
            body_hash="h",
            payload={},
        )

    adapter, _ = build(success_handler, store_id=OTHER_STORE)
    with pytest.raises(NotConfigured):
        await adapter.send(
            tenant_id=TENANT,
            channel_account_id=ACCOUNT,
            idempotency_key="k",
            body_hash="h",
            payload={},
        )

    adapter, _ = build(success_handler, store_id=None)
    with pytest.raises(NotConfigured):
        await adapter.lookup_receipt(ACCOUNT, "k")


@pytest.mark.unit
async def test_missing_credentials_is_not_configured():
    adapter, _ = build(success_handler, channel_api_key="")
    with pytest.raises(NotConfigured) as excinfo:
        await adapter.send(
            tenant_id=TENANT,
            channel_account_id=ACCOUNT,
            idempotency_key="k",
            body_hash="h",
            payload={},
        )
    assert "CHANNEL_API_KEY" in str(excinfo.value)