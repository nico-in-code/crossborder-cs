from __future__ import annotations

import uuid
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import BizInventory, BizOrder, BizShipment


class CommerceAdapter(Protocol):
    async def recent_orders(
        self, tenant_id: uuid.UUID, store_ids: set[uuid.UUID], buyer_external_id: str, limit: int
    ) -> list[dict]: ...

    async def get_order(
        self, tenant_id: uuid.UUID, store_ids: set[uuid.UUID], order_no: str
    ) -> dict | None: ...

    async def list_shipments(
        self, tenant_id: uuid.UUID, store_ids: set[uuid.UUID], order_id: uuid.UUID
    ) -> list[dict]: ...

    async def track_shipment(
        self, tenant_id: uuid.UUID, store_ids: set[uuid.UUID], tracking_no: str
    ) -> dict | None: ...

    async def get_inventory(
        self, tenant_id: uuid.UUID, store_ids: set[uuid.UUID], sku: str
    ) -> dict | None: ...


def _order_dict(order: BizOrder) -> dict:
    return {
        "order_id": str(order.id),
        "order_no": order.order_no,
        "status": order.status,
        "buyer_external_id": order.buyer_external_id,
        "total_amount": float(order.total_amount),
        "currency": order.currency,
        "placed_at": order.placed_at.isoformat() if order.placed_at else None,
        "items": order.items,
        "shipping_address": order.shipping_address,
        "store_code": None,
    }


def _shipment_dict(shipment: BizShipment) -> dict:
    return {
        "shipment_id": str(shipment.id),
        "order_id": str(shipment.order_id),
        "carrier": shipment.carrier,
        "tracking_no": shipment.tracking_no,
        "delivery_status": shipment.delivery_status,
        "delivered_at": shipment.delivered_at.isoformat() if shipment.delivered_at else None,
        "events": shipment.events,
        "last_synced_at": (
            shipment.last_synced_at.isoformat() if shipment.last_synced_at else None
        ),
    }


class DbCommerceAdapter:
    """Reads local business replica tables through the read-only database role."""

    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]):
        self.sessionmaker = sessionmaker

    async def recent_orders(
        self, tenant_id: uuid.UUID, store_ids: set[uuid.UUID], buyer_external_id: str, limit: int
    ) -> list[dict]:
        async with self.sessionmaker() as session:
            rows = await session.scalars(
                select(BizOrder)
                .where(
                    BizOrder.tenant_id == tenant_id,
                    BizOrder.store_id.in_(store_ids),
                    BizOrder.buyer_external_id == buyer_external_id,
                )
                .order_by(BizOrder.placed_at.desc())
                .limit(limit)
            )
            return [_order_dict(order) for order in rows]

    async def get_order(
        self, tenant_id: uuid.UUID, store_ids: set[uuid.UUID], order_no: str
    ) -> dict | None:
        async with self.sessionmaker() as session:
            order = await session.scalar(
                select(BizOrder).where(
                    BizOrder.tenant_id == tenant_id,
                    BizOrder.store_id.in_(store_ids),
                    BizOrder.order_no == order_no,
                )
            )
            return _order_dict(order) if order else None

    async def list_shipments(
        self, tenant_id: uuid.UUID, store_ids: set[uuid.UUID], order_id: uuid.UUID
    ) -> list[dict]:
        async with self.sessionmaker() as session:
            rows = await session.scalars(
                select(BizShipment)
                .join(BizOrder, BizShipment.order_id == BizOrder.id)
                .where(
                    BizShipment.tenant_id == tenant_id,
                    BizShipment.store_id.in_(store_ids),
                    BizOrder.tenant_id == tenant_id,
                    BizOrder.store_id.in_(store_ids),
                    BizShipment.order_id == order_id,
                )
            )
            return [_shipment_dict(shipment) for shipment in rows]

    async def track_shipment(
        self, tenant_id: uuid.UUID, store_ids: set[uuid.UUID], tracking_no: str
    ) -> dict | None:
        async with self.sessionmaker() as session:
            shipment = await session.scalar(
                select(BizShipment).where(
                    BizShipment.tenant_id == tenant_id,
                    BizShipment.store_id.in_(store_ids),
                    BizShipment.tracking_no == tracking_no,
                )
            )
            return _shipment_dict(shipment) if shipment else None

    async def get_inventory(
        self, tenant_id: uuid.UUID, store_ids: set[uuid.UUID], sku: str
    ) -> dict | None:
        async with self.sessionmaker() as session:
            row = await session.scalar(
                select(BizInventory).where(
                    BizInventory.tenant_id == tenant_id,
                    BizInventory.store_id.in_(store_ids),
                    BizInventory.sku == sku,
                )
            )
            if row is None:
                return None
            return {
                "sku": row.sku,
                "quantity": row.quantity,
                "updated_at": row.updated_at.isoformat() if row.updated_at else None,
            }


class ScopeResolver:
    """Authorization checks that run before any downstream commerce adapter call."""

    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]):
        self.sessionmaker = sessionmaker

    async def order_in_scope(
        self, tenant_id: uuid.UUID, store_ids: set[uuid.UUID], order_no: str
    ) -> bool:
        async with self.sessionmaker() as session:
            return (
                await session.scalar(
                    select(BizOrder.id).where(
                        BizOrder.tenant_id == tenant_id,
                        BizOrder.store_id.in_(store_ids),
                        BizOrder.order_no == order_no,
                    )
                )
            ) is not None

    async def order_id_in_scope(
        self, tenant_id: uuid.UUID, store_ids: set[uuid.UUID], order_id: uuid.UUID
    ) -> bool:
        async with self.sessionmaker() as session:
            return (
                await session.scalar(
                    select(BizOrder.id).where(
                        BizOrder.tenant_id == tenant_id,
                        BizOrder.store_id.in_(store_ids),
                        BizOrder.id == order_id,
                    )
                )
            ) is not None

    async def tracking_in_scope(
        self, tenant_id: uuid.UUID, store_ids: set[uuid.UUID], tracking_no: str
    ) -> bool:
        async with self.sessionmaker() as session:
            return (
                await session.scalar(
                    select(BizShipment.id).where(
                        BizShipment.tenant_id == tenant_id,
                        BizShipment.store_id.in_(store_ids),
                        BizShipment.tracking_no == tracking_no,
                    )
                )
            ) is not None

    async def sku_in_scope(
        self, tenant_id: uuid.UUID, store_ids: set[uuid.UUID], sku: str
    ) -> bool:
        async with self.sessionmaker() as session:
            return (
                await session.scalar(
                    select(BizInventory.id).where(
                        BizInventory.tenant_id == tenant_id,
                        BizInventory.store_id.in_(store_ids),
                        BizInventory.sku == sku,
                    )
                )
            ) is not None