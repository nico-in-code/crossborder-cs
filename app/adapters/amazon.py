from __future__ import annotations

import asyncio
import hashlib
import hmac
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl, quote, urlencode, urlparse, urlunparse

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

PENDING_LABEL = "关联待核实"
LINKED_LABEL = "已关联"


def _sign(key: bytes, message: str) -> bytes:
    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()


def _signing_key(secret: str, date_stamp: str, region: str, service: str) -> bytes:
    date_key = _sign(("AWS4" + secret).encode("utf-8"), date_stamp)
    region_key = _sign(date_key, region)
    service_key = _sign(region_key, service)
    return _sign(service_key, "aws4_request")


def sign_headers(
    method: str,
    url: str,
    headers: dict[str, str],
    body: bytes = b"",
    *,
    access_key: str,
    secret_key: str,
    region: str,
    service: str = "execute-api",
    session_token: str = "",
    now: datetime | None = None,
) -> tuple[str, dict[str, str]]:
    """Return the canonicalized URL and headers with an AWS SigV4 Authorization header."""
    parsed = urlparse(url)
    query = urlencode(sorted(parse_qsl(parsed.query, keep_blank_values=True)))
    canonical_uri = parsed.path or "/"
    if query:
        url = urlunparse(parsed._replace(query=query))
    moment = now or datetime.now(timezone.utc)
    amz_date = moment.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = moment.strftime("%Y%m%d")

    signed = {key.lower(): value for key, value in headers.items()}
    signed["host"] = parsed.netloc
    signed["x-amz-date"] = amz_date
    if session_token:
        signed["x-amz-security-token"] = session_token
    signed_names = sorted(signed)
    canonical_headers = "".join(f"{name}:{signed[name].strip()}\n" for name in signed_names)
    payload_hash = hashlib.sha256(body or b"").hexdigest()
    canonical_request = "\n".join(
        [
            method.upper(),
            canonical_uri,
            query,
            canonical_headers,
            ";".join(signed_names),
            payload_hash,
        ]
    )
    scope = f"{date_stamp}/{region}/{service}/aws4_request"
    string_to_sign = "\n".join(
        [
            "AWS4-HMAC-SHA256",
            amz_date,
            scope,
            hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
        ]
    )
    signature = hmac.new(
        _signing_key(secret_key, date_stamp, region, service),
        string_to_sign.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    signed["authorization"] = (
        f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, "
        f"SignedHeaders={';'.join(signed_names)}, Signature={signature}"
    )
    return url, signed


class LwaTokenProvider:
    """Caches the LWA access token until shortly before it expires."""

    def __init__(self, settings: Settings, client: httpx.AsyncClient):
        self.settings = settings
        self.client = client
        self._token = ""
        self._expires_at = datetime.min.replace(tzinfo=timezone.utc)
        self._lock = asyncio.Lock()

    async def token(self) -> str:
        now = datetime.now(timezone.utc)
        if self._token and now < self._expires_at:
            return self._token
        async with self._lock:
            now = datetime.now(timezone.utc)
            if self._token and now < self._expires_at:
                return self._token
            try:
                response = await self.client.post(
                    self.settings.amazon_lwa_endpoint,
                    data={
                        "grant_type": "refresh_token",
                        "refresh_token": self.settings.amazon_refresh_token,
                        "client_id": self.settings.amazon_lwa_client_id,
                        "client_secret": self.settings.amazon_lwa_client_secret,
                    },
                    headers={"content-type": "application/x-www-form-urlencoded"},
                )
            except httpx.TimeoutException as exc:
                raise UpstreamTimeout("LWA token request timed out") from exc
            if response.status_code in {400, 401, 403}:
                raise PermissionDenied(f"LWA rejected credentials ({response.status_code})")
            if response.status_code >= 500:
                raise UpstreamError(f"LWA returned {response.status_code}")
            if response.status_code >= 400:
                raise UpstreamError(f"LWA returned unexpected status {response.status_code}")
            payload = response.json()
            token = payload.get("access_token") or ""
            if not token:
                raise UpstreamError("LWA response did not contain an access token")
            expires_in = int(payload.get("expires_in") or 3600)
            self._token = token
            self._expires_at = datetime.now(timezone.utc) + timedelta(
                seconds=max(expires_in - 60, 30)
            )
            return token


class AmazonSpApiAdapter:
    """Amazon SP-API order reads with LWA and SigV4; disabled unless a store is enabled."""

    def __init__(
        self,
        settings: Settings,
        *,
        client: httpx.AsyncClient | None = None,
        token_provider: LwaTokenProvider | None = None,
    ):
        self.settings = settings
        self.client = client or httpx.AsyncClient(timeout=10.0)
        self.token_provider = token_provider or LwaTokenProvider(settings, self.client)
        self.enablement = StoreEnablement.from_settings(
            settings.integration_enabled_store_ids
        )

    def _ensure(self, store_ids: set[uuid.UUID] | list[uuid.UUID]) -> uuid.UUID:
        return ensure_ready(
            name="amazon_sp_api",
            flag=self.settings.amazon_sp_api_enabled,
            credentials={
                "AMAZON_LWA_CLIENT_ID": self.settings.amazon_lwa_client_id,
                "AMAZON_LWA_CLIENT_SECRET": self.settings.amazon_lwa_client_secret,
                "AMAZON_REFRESH_TOKEN": self.settings.amazon_refresh_token,
                "AWS_ACCESS_KEY_ID": self.settings.aws_access_key_id,
                "AWS_SECRET_ACCESS_KEY": self.settings.aws_secret_access_key,
            },
            store_ids=store_ids,
            enablement=self.enablement,
        )

    async def _get(
        self,
        store_id: uuid.UUID,
        path: str,
        params: dict[str, str] | None = None,
        *,
        missing_ok: bool = False,
    ) -> dict | None:
        token = await self.token_provider.token()
        url = f"{self.settings.amazon_endpoint.rstrip('/')}{path}"
        if params:
            url = f"{url}?{urlencode(sorted(params.items()))}"
        signed_url, headers = sign_headers(
            "GET",
            url,
            {"x-amz-access-token": token, "accept": "application/json"},
            access_key=self.settings.aws_access_key_id,
            secret_key=self.settings.aws_secret_access_key,
            region=self.settings.amazon_region,
            service="execute-api",
            session_token=self.settings.aws_session_token,
        )
        try:
            response = await self.client.get(signed_url, headers=headers)
        except httpx.TimeoutException as exc:
            raise UpstreamTimeout(f"SP-API {path} timed out") from exc
        if response.status_code in {401, 403}:
            raise PermissionDenied(
                f"SP-API denied access to {path} ({response.status_code})"
            )
        if response.status_code == 404 and missing_ok:
            return None
        if response.status_code >= 500:
            raise UpstreamError(f"SP-API {path} returned {response.status_code}")
        if response.status_code >= 400:
            raise UpstreamError(
                f"SP-API {path} returned unexpected status {response.status_code}"
            )
        return response.json()

    async def _order_items(
        self, store_id: uuid.UUID, order_no: str
    ) -> tuple[list[dict], str | None]:
        try:
            payload = await self._get(
                store_id,
                f"/orders/v0/orders/{quote(order_no, safe='')}/orderItems",
                missing_ok=True,
            )
        except PermissionDenied:
            return [], "order_items_forbidden"
        if payload is None:
            return [], "order_items_missing"
        rows = (payload.get("payload") or {}).get("OrderItems") or []
        items = []
        for row in rows:
            price = (row.get("ItemPrice") or {}).get("Amount")
            items.append(
                {
                    "sku": row.get("SellerSKU"),
                    "asin": row.get("ASIN"),
                    "name": row.get("Title"),
                    "quantity": int(row.get("QuantityOrdered") or 0),
                    "unit_price": float(price) if price is not None else None,
                }
            )
        return items, None if items else "order_items_missing"

    async def _shipment(
        self, store_id: uuid.UUID, order_no: str
    ) -> tuple[dict | None, str | None]:
        try:
            payload = await self._get(
                store_id,
                f"/orders/v0/orders/{quote(order_no, safe='')}/shipment",
                missing_ok=True,
            )
        except PermissionDenied:
            return None, "shipment_not_exposed"
        if payload is None:
            return None, "shipment_not_exposed"
        shipment = payload.get("payload") or payload
        tracking = shipment.get("trackingNumber") or shipment.get("TrackingNumber")
        if not tracking:
            return None, "shipment_not_exposed"
        return (
            {
                "shipment_id": shipment.get("shipmentId") or shipment.get("ShipmentId"),
                "carrier": shipment.get("carrier") or shipment.get("Carrier"),
                "tracking_no": tracking,
                "delivery_status": shipment.get("status")
                or shipment.get("DeliveryStatus")
                or "unknown",
                "events": shipment.get("events") or shipment.get("Events") or [],
                "last_synced_at": datetime.now(timezone.utc).isoformat(),
            },
            None,
        )

    @staticmethod
    def _shipping_address(raw: dict) -> dict:
        address = raw.get("ShippingAddress") or {}
        return {
            "country": address.get("CountryCode"),
            "prefecture": address.get("StateOrRegion"),
            "city": address.get("City"),
            "masked": "***" if address else None,
        }

    @staticmethod
    def _association_reason(address: dict, items: list[dict], shipment: dict | None) -> str | None:
        if not items:
            return "order_items_missing"
        if not address.get("country"):
            return "shipping_address_incomplete"
        if address.get("country") == "JP" and not address.get("prefecture"):
            return "shipping_address_incomplete"
        if shipment is None:
            return "shipment_not_exposed"
        return None

    async def _normalize_order(self, store_id: uuid.UUID, raw: dict) -> dict:
        order_no = raw.get("AmazonOrderId") or ""
        buyer = raw.get("BuyerInfo") or {}
        items, items_reason = await self._order_items(store_id, order_no)
        shipment, shipment_reason = await self._shipment(store_id, order_no)
        address = self._shipping_address(raw)
        reason = (
            items_reason
            if items_reason == "order_items_forbidden"
            else shipment_reason
            if shipment_reason and items
            else self._association_reason(address, items, shipment)
        )
        association = {
            "status": "pending_verification" if reason else "linked",
            "label": PENDING_LABEL if reason else LINKED_LABEL,
            "reason": reason,
        }
        total = raw.get("OrderTotal") or {}
        return {
            "order_no": order_no,
            "buyer_external_id": buyer.get("BuyerExternalId") or buyer.get("BuyerEmail"),
            "status": raw.get("OrderStatus") or "unknown",
            "currency": total.get("CurrencyCode"),
            "total_amount": float(total["Amount"]) if total.get("Amount") is not None else None,
            "placed_at": raw.get("PurchaseDate"),
            "marketplace_ids": [raw.get("MarketplaceId")] if raw.get("MarketplaceId") else [],
            "items": items,
            "shipping_address": address,
            "shipment": shipment,
            "association": association,
        }

    async def list_orders(
        self,
        tenant_id: uuid.UUID,
        store_ids: set[uuid.UUID] | list[uuid.UUID],
        *,
        buyer_external_id: str | None = None,
        created_after: str | None = None,
        limit: int = 20,
    ) -> list[dict]:
        store_id = self._ensure(store_ids)
        marketplaces = [
            part.strip()
            for part in (self.settings.amazon_marketplace_ids or "").split(",")
            if part.strip()
        ]
        if not marketplaces:
            raise NotConfigured("AMAZON_MARKETPLACE_IDS is not configured")
        params = {
            "MarketplaceIds": ",".join(marketplaces),
            "MaxResultsPerPage": str(limit),
        }
        if created_after:
            params["CreatedAfter"] = created_after
        payload = await self._get(store_id, "/orders/v0/orders", params)
        orders = ((payload or {}).get("payload") or {}).get("Orders") or []
        results = []
        for raw in orders:
            normalized = await self._normalize_order(store_id, raw)
            if buyer_external_id and normalized["buyer_external_id"] != buyer_external_id:
                continue
            results.append(normalized)
        return results

    async def get_order(
        self,
        tenant_id: uuid.UUID,
        store_ids: set[uuid.UUID] | list[uuid.UUID],
        order_no: str,
    ) -> dict | None:
        store_id = self._ensure(store_ids)
        payload = await self._get(
            store_id,
            f"/orders/v0/orders/{quote(order_no, safe='')}",
            missing_ok=True,
        )
        if payload is None:
            return None
        raw = (payload.get("payload") or {})
        if not raw:
            return None
        return await self._normalize_order(store_id, raw)

    async def list_shipments(
        self,
        tenant_id: uuid.UUID,
        store_ids: set[uuid.UUID] | list[uuid.UUID],
        order_no: str,
    ) -> list[dict]:
        order = await self.get_order(tenant_id, store_ids, order_no)
        if order is None or order.get("shipment") is None:
            return []
        return [order["shipment"]]