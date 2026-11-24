from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import delete, select

from app.adapters.commerce import DbCommerceAdapter, ScopeResolver
from app.config import Settings
from app.models import Draft, Employee, KnowledgeDoc, Tenant
from app.services.pipeline import default_adapters, process_ticket

from stage2_helpers import SAMPLE_TEXT, create_ticket


class SlowTrackingCommerce:
    def __init__(self, inner: DbCommerceAdapter):
        self.inner = inner

    async def recent_orders(self, *args):
        return await self.inner.recent_orders(*args)

    async def get_order(self, *args):
        return await self.inner.get_order(*args)

    async def list_shipments(self, *args):
        return await self.inner.list_shipments(*args)

    async def track_shipment(self, *args):
        await asyncio.sleep(5.0)
        return await self.inner.track_shipment(*args)

    async def get_inventory(self, *args):
        return await self.inner.get_inventory(*args)


@pytest.mark.integration
async def test_timeout_and_missing_policy_keep_verified_facts(
    client, seeded, inbound_sender, db, settings: Settings, test_sessionmaker
):
    ticket = await create_ticket(client, inbound_sender, "JP-STAGE2-TIMEOUT")
    tenant_a = await db.scalar(select(Tenant).where(Tenant.code == "A"))
    agent = await db.scalar(select(Employee).where(Employee.email == "cs.a@example.com"))

    await db.execute(delete(KnowledgeDoc).where(KnowledgeDoc.tenant_id == tenant_a.id))
    await db.commit()

    timeout_settings = settings.model_copy(
        update={"mcp_tool_timeout_seconds": 2.0, "agent_total_timeout_seconds": 10.0}
    )
    result = await process_ticket(
        test_sessionmaker,
        timeout_settings,
        ticket["ticket_id"],
        agent.id,
        adapters=default_adapters(),
        commerce=SlowTrackingCommerce(DbCommerceAdapter(test_sessionmaker)),
        resolver=ScopeResolver(test_sessionmaker),
    )

    assert result["route"] == "business"
    assert [intent["code"] for intent in result["intents"]] == [
        "not_received",
        "refund_request",
    ]
    statuses = {call["tool_name"]: call["status"] for call in result["tool_calls"]}
    assert statuses["order_get"] == "ok"
    assert statuses["shipments_list"] == "ok"
    assert statuses["shipment_track"] == "timeout"
    assert result["evidence_count"] == 0
    assert any("物流" in gap or "超时" in gap for gap in result["gaps"])
    assert any("政策" in gap for gap in result["gaps"])

    db.expire_all()
    draft = await db.scalar(
        select(Draft).where(Draft.ticket_id == ticket["ticket_id"])
    )
    facts = draft.structured_facts
    assert facts["buyer_claims"]["original_text"] == SAMPLE_TEXT
    assert facts["buyer_claims"]["translated_text"]
    assert facts["carrier_records"]["order_no"] == "A-JP-1001"
    assert facts["carrier_records"]["tracking_no"] == "YMT-A-0001"
    assert facts["carrier_records"]["carrier"] == "Yamato"
    assert facts["carrier_records"]["delivery_status"] == "delivered"
    verify_codes = {item["code"] for item in facts["to_verify"]}
    assert {"carrier_investigation", "refund_followup", "tracking_recheck", "policy_gap"} <= (
        verify_codes
    )
    assert facts["policy_evidence"] == []
    assert "【未確認事項】" in draft.body
    assert "配送会社への照会が完了していません。" in draft.body
    assert "YMT-A-0001" in draft.body