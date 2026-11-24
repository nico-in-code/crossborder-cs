from __future__ import annotations

import uuid
from datetime import datetime, timezone

import httpx
import pytest

from app.adapters.amazon import AmazonSpApiAdapter, sign_headers
from app.adapters.integrations import (
    NotConfigured,
    PermissionDenied,
    UpstreamError,
    UpstreamTimeout,
)
from app.config import Settings

STORE = uuid.uuid4()
TENANT = uuid.uuid4()

BASE_ORDER = {
    "AmazonOrderId": "A-JP-9001",
    "PurchaseDate": "2026-11-20T00:00:00Z",
    "OrderStatus": "Shipped",
    "MarketplaceId": "A1VC38T7YXB528",
    "OrderTotal": {"CurrencyCode": "JPY", "Amount": "3200"},
    "BuyerInfo": {"BuyerEmail": "buyer@example.com"},
    "ShippingAddress": {"CountryCode": "JP", "StateOrRegion": "東京都", "City": "渋谷区"},
}

ORDER_ITEMS = {
    "payload": {
        "OrderItems": [
            {
                "SellerSKU": "SKU-JP-001",
                "ASIN": "B000000001",
                "Title": "折りたたみ傘",
                "QuantityOrdered": 1,
                "ItemPrice": {"Amount": "3200"},
            }
        ]
    }
}

SHIPMENT = {
    "payload": {
        "shipmentId": "shp-1",
        "carrier": "Yamato",
        "trackingNumber": "YMT-A-0001",
        "status": "delivered",
        "events": [{"status": "delivered", "at": "2026-11-21T00:00:00Z"}],
    }
}


def make_settings(**overrides) -> Settings:
    values = {
        "amazon_sp_api_enabled": True,
        "amazon_lwa_client_id": "lwa-client",
        "amazon_lwa_client_secret": "lwa-secret",
        "amazon_refresh_token": "refresh-token",
        "amazon_region": "us-east-1",
        "amazon_endpoint": "https://sellingpartnerapi-fe.amazon.com",
        "amazon_lwa_endpoint": "https://api.amazon.com/auth/o2/token",
        "amazon_marketplace_ids": "A1VC38T7YXB528",
        "aws_access_key_id": "AKIDEXAMPLE",
        "aws_secret_access_key": "SECRETEXAMPLE",
        "aws_session_token": "SESSIONTOKEN",
        "integration_enabled_store_ids": str(STORE),
    }
    values.update(overrides)
    return Settings(**values)


def make_adapter(handler, **overrides) -> tuple[AmazonSpApiAdapter, list[httpx.Request]]:
    requests: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handler(request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(wrapped))
    return AmazonSpApiAdapter(make_settings(**overrides), client=client), requests


def full_handler(request: httpx.Request) -> httpx.Response:
    if request.url.host == "api.amazon.com":
        return httpx.Response(
            200, json={"access_token": "Atza|token", "expires_in": 3600}
        )
    path = request.url.path
    if path == "/orders/v0/orders/A-JP-9001":
        return httpx.Response(200, json={"payload": BASE_ORDER})
    if path.endswith("/orderItems"):
        return httpx.Response(200, json=ORDER_ITEMS)
    if path.endswith("/shipment"):
        return httpx.Response(200, json=SHIPMENT)
    return httpx.Response(404, json={"errors": []})


@pytest.mark.unit
async def test_disabled_or_unscoped_store_is_not_configured():
    adapter, _ = make_adapter(full_handler, amazon_sp_api_enabled=False)
    with pytest.raises(NotConfigured):
        await adapter.get_order(TENANT, {STORE}, "A-JP-9001")

    adapter, _ = make_adapter(full_handler, integration_enabled_store_ids="")
    with pytest.raises(NotConfigured):
        await adapter.get_order(TENANT, {STORE}, "A-JP-9001")


@pytest.mark.unit
async def test_missing_credentials_is_not_configured():
    adapter, _ = make_adapter(full_handler, amazon_refresh_token="")
    with pytest.raises(NotConfigured) as excinfo:
        await adapter.get_order(TENANT, {STORE}, "A-JP-9001")
    assert "AMAZON_REFRESH_TOKEN" in str(excinfo.value)


@pytest.mark.unit
async def test_success_uses_lwa_and_sigv4_and_normalizes_order():
    adapter, requests = make_adapter(full_handler)
    order = await adapter.get_order(TENANT, {STORE}, "A-JP-9001")

    lwa = requests[0]
    assert lwa.url.host == "api.amazon.com"
    assert b"grant_type=refresh_token" in lwa.content

    spapi_calls = [request for request in requests if request.url.host != "api.amazon.com"]
    assert len(spapi_calls) == 3
    for request in spapi_calls:
        assert request.headers["x-amz-access-token"] == "Atza|token"
        assert request.headers["authorization"].startswith("AWS4-HMAC-SHA256 ")
        assert "SignedHeaders=" in request.headers["authorization"]
        assert request.headers["x-amz-date"]
        assert request.headers["x-amz-security-token"] == "SESSIONTOKEN"

    assert order["order_no"] == "A-JP-9001"
    assert order["buyer_external_id"] == "buyer@example.com"
    assert order["total_amount"] == 3200.0
    assert order["currency"] == "JPY"
    assert order["items"][0]["sku"] == "SKU-JP-001"
    assert order["shipping_address"]["prefecture"] == "東京都"
    assert order["shipping_address"]["masked"] == "***"
    assert order["shipment"]["tracking_no"] == "YMT-A-0001"
    assert order["association"] == {"status": "linked", "label": "已关联", "reason": None}


@pytest.mark.unit
async def test_insufficient_response_marks_association_pending():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.amazon.com":
            return httpx.Response(200, json={"access_token": "token", "expires_in": 3600})
        if request.url.path == "/orders/v0/orders/A-JP-9001":
            return httpx.Response(200, json={"payload": BASE_ORDER})
        return httpx.Response(404, json={"errors": []})

    adapter, _ = make_adapter(handler)
    order = await adapter.get_order(TENANT, {STORE}, "A-JP-9001")
    assert order["association"]["status"] == "pending_verification"
    assert order["association"]["label"] == "关联待核实"
    assert order["association"]["reason"] == "order_items_missing"
    assert order["items"] == []
    assert order["shipment"] is None


@pytest.mark.unit
async def test_missing_shipment_marks_pending_even_with_items():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.amazon.com":
            return httpx.Response(200, json={"access_token": "token", "expires_in": 3600})
        if request.url.path == "/orders/v0/orders/A-JP-9001":
            return httpx.Response(200, json={"payload": BASE_ORDER})
        if request.url.path.endswith("/orderItems"):
            return httpx.Response(200, json=ORDER_ITEMS)
        return httpx.Response(404, json={"errors": []})

    adapter, _ = make_adapter(handler)
    order = await adapter.get_order(TENANT, {STORE}, "A-JP-9001")
    assert order["association"]["status"] == "pending_verification"
    assert order["association"]["reason"] == "shipment_not_exposed"
    assert order["items"][0]["sku"] == "SKU-JP-001"


@pytest.mark.unit
async def test_list_orders_sends_marketplace_and_filters_buyer():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.amazon.com":
            return httpx.Response(200, json={"access_token": "token", "expires_in": 3600})
        if request.url.path == "/orders/v0/orders":
            other = {
                **BASE_ORDER,
                "AmazonOrderId": "A-JP-9002",
                "BuyerInfo": {"BuyerEmail": "other@example.com"},
            }
            return httpx.Response(
                200, json={"payload": {"Orders": [BASE_ORDER, other]}}
            )
        return httpx.Response(404, json={"errors": []})

    adapter, requests = make_adapter(handler)
    orders = await adapter.list_orders(
        TENANT, {STORE}, buyer_external_id="buyer@example.com"
    )
    assert [order["order_no"] for order in orders] == ["A-JP-9001"]
    search = requests[1]
    assert "MarketplaceIds=A1VC38T7YXB528" in str(search.url)
    assert "MaxResultsPerPage=20" in str(search.url)


@pytest.mark.unit
async def test_permission_error_maps_to_permission_denied():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.amazon.com":
            return httpx.Response(200, json={"access_token": "token", "expires_in": 3600})
        return httpx.Response(403, json={"errors": [{"code": "Unauthorized"}]})

    adapter, _ = make_adapter(handler)
    with pytest.raises(PermissionDenied):
        await adapter.get_order(TENANT, {STORE}, "A-JP-9001")


@pytest.mark.unit
async def test_server_error_maps_to_upstream_error():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.amazon.com":
            return httpx.Response(200, json={"access_token": "token", "expires_in": 3600})
        return httpx.Response(500, text="boom")

    adapter, _ = make_adapter(handler)
    with pytest.raises(UpstreamError):
        await adapter.get_order(TENANT, {STORE}, "A-JP-9001")


@pytest.mark.unit
async def test_timeout_maps_to_upstream_timeout():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.amazon.com":
            return httpx.Response(200, json={"access_token": "token", "expires_in": 3600})
        raise httpx.ConnectTimeout("timed out", request=request)

    adapter, _ = make_adapter(handler)
    with pytest.raises(UpstreamTimeout):
        await adapter.get_order(TENANT, {STORE}, "A-JP-9001")


@pytest.mark.unit
async def test_lwa_rejection_maps_to_permission_denied():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "invalid_grant"})

    adapter, _ = make_adapter(handler)
    with pytest.raises(PermissionDenied):
        await adapter.get_order(TENANT, {STORE}, "A-JP-9001")


@pytest.mark.unit
def test_sigv4_signature_depends_on_secret_and_is_canonical():
    fixed = datetime(2026, 11, 20, 1, 2, 3, tzinfo=timezone.utc)
    url, headers = sign_headers(
        "GET",
        "https://sellingpartnerapi-fe.amazon.com/orders/v0/orders?Z=2&A=1",
        {"x-amz-access-token": "token"},
        access_key="AKID",
        secret_key="secret-one",
        region="us-east-1",
        now=fixed,
    )
    assert url.endswith("?A=1&Z=2")
    assert headers["authorization"].startswith("AWS4-HMAC-SHA256 Credential=AKID/")
    assert "SignedHeaders=host;x-amz-access-token;x-amz-date" in headers["authorization"]

    _, other = sign_headers(
        "GET",
        "https://sellingpartnerapi-fe.amazon.com/orders/v0/orders?Z=2&A=1",
        {"x-amz-access-token": "token"},
        access_key="AKID",
        secret_key="secret-two",
        region="us-east-1",
        now=fixed,
    )
    assert headers["authorization"] != other["authorization"]