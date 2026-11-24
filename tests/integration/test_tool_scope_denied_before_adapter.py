from __future__ import annotations

import uuid

import pytest
from sqlalchemy import make_url, select

from app.adapters.commerce import DbCommerceAdapter, ScopeResolver
from app.db import build_engine, build_sessionmaker
from app.mcp.client import ScopedToolClient, open_readonly_session
from app.mcp.context import BUSINESS_TOOL_NAMES, RunContext, run_context_token
from app.mcp.tools_readonly import TOOL_SPECS
from app.models import Employee, Store, Tenant, ToolCallStatus


class SpyCommerce:
    def __init__(self, inner: DbCommerceAdapter):
        self.inner = inner
        self.calls: list[tuple[str, object]] = []

    async def recent_orders(self, *args):
        self.calls.append(("recent_orders", args[-2]))
        return await self.inner.recent_orders(*args)

    async def get_order(self, *args):
        self.calls.append(("get_order", args[-1]))
        return await self.inner.get_order(*args)

    async def list_shipments(self, *args):
        self.calls.append(("list_shipments", args[-1]))
        return await self.inner.list_shipments(*args)

    async def track_shipment(self, *args):
        self.calls.append(("track_shipment", args[-1]))
        return await self.inner.track_shipment(*args)

    async def get_inventory(self, *args):
        self.calls.append(("get_inventory", args[-1]))
        return await self.inner.get_inventory(*args)


@pytest.mark.integration
async def test_out_of_scope_order_denied_before_adapter(settings, seeded, db):
    tenant_a = await db.scalar(select(Tenant).where(Tenant.code == "A"))
    store_a = await db.scalar(select(Store).where(Store.code == "A-JP-01"))
    agent = await db.scalar(select(Employee).where(Employee.email == "cs.a@example.com"))

    mcp_url = (
        make_url(settings.mcp_database_url)
        .set(database=make_url(settings.test_database_url).database)
        .render_as_string(hide_password=False)
    )
    engine = build_engine(settings, url=mcp_url)
    mcp_sessionmaker = build_sessionmaker(engine)
    spy = SpyCommerce(DbCommerceAdapter(mcp_sessionmaker))
    resolver = ScopeResolver(mcp_sessionmaker)

    def token_for(role: str = "agent") -> str:
        return run_context_token(
            settings,
            RunContext(
                tenant_id=tenant_a.id,
                store_ids={store_a.id},
                employee_id=str(agent.id),
                role=role,
                allowed_tools=BUSINESS_TOOL_NAMES,
                ticket_id=str(uuid.uuid4()),
                graph="master",
            ),
        )

    try:
        async with open_readonly_session(settings, spy, resolver) as mcp_session:
            client = ScopedToolClient(settings)

            other_store = await client.call(
                mcp_session, token_for(), "order_get", {"order_no": "A2-JP-2001"}
            )
            other_tenant = await client.call(
                mcp_session, token_for(), "order_get", {"order_no": "B-JP-3001"}
            )
            forged_identity = await client.call(
                mcp_session,
                token_for(),
                "order_get",
                {"order_no": "A-JP-1001", "tenant_id": str(tenant_a.id)},
            )
            forbidden_role = await client.call(
                mcp_session, token_for("knowledge_admin"), "order_get", {"order_no": "A-JP-1001"}
            )

            assert other_store.status is ToolCallStatus.denied
            assert other_store.denied_reason == "resource_out_of_scope"
            assert other_tenant.status is ToolCallStatus.denied
            assert other_tenant.denied_reason == "resource_out_of_scope"
            assert forged_identity.status in {ToolCallStatus.denied, ToolCallStatus.error}
            assert forbidden_role.status is ToolCallStatus.denied
            assert forbidden_role.denied_reason == "role_not_allowed"
            assert spy.calls == []

            in_scope = await client.call(
                mcp_session, token_for(), "order_get", {"order_no": "A-JP-1001"}
            )
            assert in_scope.status is ToolCallStatus.ok
            assert spy.calls == [("get_order", "A-JP-1001")]
    finally:
        await engine.dispose()

    for spec in TOOL_SPECS.values():
        properties = set(spec["inputSchema"]["properties"])
        assert not properties & {"tenant_id", "store_id", "employee_id", "role"}