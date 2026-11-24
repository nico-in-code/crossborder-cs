from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models import (
    BizInventory,
    BizOrder,
    BizShipment,
    ChannelAccount,
    Employee,
    EmployeeRole,
    Faq,
    KnowledgeChunk,
    KnowledgeDoc,
    KnowledgeStatus,
    KnowledgeVersion,
    Sop,
    SopVersion,
    Store,
    StoreGrant,
    Tenant,
    TrainingCase,
    TrainingStatus,
)
from app.security import content_hash, hash_password


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def seed_all(session: AsyncSession, settings: Settings) -> dict[str, int]:
    stats = {
        "tenants": 0,
        "stores": 0,
        "employees": 0,
        "grants": 0,
        "channels": 0,
        "orders": 0,
        "shipments": 0,
        "inventory": 0,
        "knowledge_docs": 0,
        "faqs": 0,
        "sops": 0,
        "training_cases": 0,
    }

    async def upsert_tenant(code: str, name: str) -> Tenant:
        tenant = (
            await session.execute(select(Tenant).where(Tenant.code == code))
        ).scalar_one_or_none()
        if tenant is None:
            tenant = Tenant(code=code, name=name)
            session.add(tenant)
            await session.flush()
            stats["tenants"] += 1
        else:
            tenant.name = name
        return tenant

    async def upsert_store(tenant: Tenant, code: str, name: str) -> Store:
        store = (
            await session.execute(
                select(Store).where(Store.tenant_id == tenant.id, Store.code == code)
            )
        ).scalar_one_or_none()
        values = {
            "tenant_id": tenant.id,
            "code": code,
            "name": name,
            "marketplace": "JP",
            "domain_id": "A1VC38T7YXB528",
            "seller_id": f"SELLER-{code}",
            "enabled": True,
            "config": {
                "language": "ja",
                "timezone": "Asia/Tokyo",
                "pilot": code != "A-JP-02",
            },
        }
        if store is None:
            store = Store(**values)
            session.add(store)
            await session.flush()
            stats["stores"] += 1
        else:
            for key, value in values.items():
                if key != "tenant_id":
                    setattr(store, key, value)
        return store

    async def upsert_employee(
        tenant: Tenant,
        email: str,
        display_name: str,
        role: EmployeeRole,
        refund_authority: bool,
    ) -> Employee:
        employee = (
            await session.execute(select(Employee).where(Employee.email == email))
        ).scalar_one_or_none()
        password_hash = hash_password(settings.seed_password)
        if employee is None:
            employee = Employee(
                tenant_id=tenant.id,
                email=email,
                display_name=display_name,
                password_hash=password_hash,
                role=role,
                refund_authority=refund_authority,
                active=True,
            )
            session.add(employee)
            await session.flush()
            stats["employees"] += 1
        else:
            employee.tenant_id = tenant.id
            employee.display_name = display_name
            employee.password_hash = password_hash
            employee.role = role
            employee.refund_authority = refund_authority
            employee.active = True
        return employee

    async def upsert_grant(tenant: Tenant, employee: Employee, store: Store) -> None:
        grant = (
            await session.execute(
                select(StoreGrant).where(
                    StoreGrant.employee_id == employee.id,
                    StoreGrant.store_id == store.id,
                )
            )
        ).scalar_one_or_none()
        if grant is None:
            session.add(
                StoreGrant(tenant_id=tenant.id, employee_id=employee.id, store_id=store.id)
            )
            stats["grants"] += 1

    async def upsert_channel(
        tenant: Tenant, store: Store, code: str, external_id: str
    ) -> ChannelAccount:
        channel = (
            await session.execute(
                select(ChannelAccount).where(ChannelAccount.code == code)
            )
        ).scalar_one_or_none()
        secret = f"{settings.test_channel_secret}-{code}"
        values = {
            "tenant_id": tenant.id,
            "store_id": store.id,
            "code": code,
            "provider": "amazon",
            "external_account_id": external_id,
            "webhook_secret": secret,
            "verified": True,
            "enabled": True,
        }
        if channel is None:
            channel = ChannelAccount(**values)
            session.add(channel)
            stats["channels"] += 1
        else:
            for key, value in values.items():
                setattr(channel, key, value)
        await session.flush()
        return channel

    async def upsert_order(
        tenant: Tenant,
        store: Store,
        order_no: str,
        buyer: str,
        total: Decimal,
        sku: str,
        item_name: str,
    ) -> BizOrder:
        order = (
            await session.execute(
                select(BizOrder).where(
                    BizOrder.tenant_id == tenant.id,
                    BizOrder.store_id == store.id,
                    BizOrder.order_no == order_no,
                )
            )
        ).scalar_one_or_none()
        values = {
            "tenant_id": tenant.id,
            "store_id": store.id,
            "order_no": order_no,
            "buyer_external_id": buyer,
            "status": "shipped",
            "total_amount": total,
            "currency": "JPY",
            "placed_at": _now() - timedelta(days=6),
            "items": [
                {
                    "sku": sku,
                    "name": item_name,
                    "quantity": 1,
                    "unit_price": float(total),
                }
            ],
            "shipping_address": {
                "country": "JP",
                "prefecture": "東京都",
                "city": "渋谷区",
                "masked": "***-****-****",
            },
        }
        if order is None:
            order = BizOrder(**values)
            session.add(order)
            await session.flush()
            stats["orders"] += 1
        else:
            for key, value in values.items():
                if key not in {"tenant_id", "store_id"}:
                    setattr(order, key, value)
        return order

    async def upsert_shipment(
        tenant: Tenant, store: Store, order: BizOrder, tracking_no: str, delivered: bool
    ) -> BizShipment:
        shipment = (
            await session.execute(
                select(BizShipment).where(
                    BizShipment.tenant_id == tenant.id,
                    BizShipment.carrier == "Yamato",
                    BizShipment.tracking_no == tracking_no,
                )
            )
        ).scalar_one_or_none()
        delivered_at = _now() - timedelta(days=1) if delivered else None
        values = {
            "tenant_id": tenant.id,
            "store_id": store.id,
            "order_id": order.id,
            "carrier": "Yamato",
            "tracking_no": tracking_no,
            "delivery_status": "delivered" if delivered else "in_transit",
            "delivered_at": delivered_at,
            "events": [
                {"status": "shipped", "at": (_now() - timedelta(days=3)).isoformat()},
                {
                    "status": "delivered" if delivered else "in_transit",
                    "at": (delivered_at or _now()).isoformat(),
                    "detail": "配達完了" if delivered else "輸送中",
                },
            ],
            "last_synced_at": _now(),
        }
        if shipment is None:
            shipment = BizShipment(**values)
            session.add(shipment)
            await session.flush()
            stats["shipments"] += 1
        else:
            for key, value in values.items():
                if key not in {"tenant_id", "store_id"}:
                    setattr(shipment, key, value)
        return shipment

    async def upsert_inventory(tenant: Tenant, store: Store, sku: str, qty: int) -> None:
        row = (
            await session.execute(
                select(BizInventory).where(
                    BizInventory.tenant_id == tenant.id,
                    BizInventory.store_id == store.id,
                    BizInventory.sku == sku,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            session.add(
                BizInventory(
                    tenant_id=tenant.id, store_id=store.id, sku=sku, quantity=qty
                )
            )
            stats["inventory"] += 1
        else:
            row.quantity = qty
            row.updated_at = _now()

    tenant_a = await upsert_tenant("A", "樱花跨境 A 公司")
    tenant_b = await upsert_tenant("B", "北辰贸易 B 公司")
    a_jp = await upsert_store(tenant_a, "A-JP-01", "A 公司日本站主店")
    a_jp_2 = await upsert_store(tenant_a, "A-JP-02", "A 公司日本站二店（未授权）")
    b_jp = await upsert_store(tenant_b, "B-JP-01", "B 公司日本站")

    cs_a = await upsert_employee(
        tenant_a, "cs.a@example.com", "客服 小林", EmployeeRole.agent, True
    )
    sup_a = await upsert_employee(
        tenant_a, "sup.a@example.com", "主管 田中", EmployeeRole.supervisor, True
    )
    await upsert_employee(
        tenant_a, "kb.a@example.com", "知识管理员 佐藤", EmployeeRole.knowledge_admin, False
    )
    await upsert_employee(
        tenant_a, "admin.a@example.com", "系统管理员 山田", EmployeeRole.system_admin, False
    )
    await upsert_employee(
        tenant_b, "cs.b@example.com", "B 公司客服", EmployeeRole.agent, True
    )
    await upsert_grant(tenant_a, cs_a, a_jp)
    await upsert_grant(tenant_a, sup_a, a_jp)
    b_agent = (
        await session.execute(
            select(Employee).where(Employee.email == "cs.b@example.com")
        )
    ).scalar_one()
    await upsert_grant(tenant_b, b_agent, b_jp)

    await upsert_channel(tenant_a, a_jp, "a-jp-01-amazon", "A-JP-SELLER-01")
    await upsert_channel(tenant_a, a_jp_2, "a-jp-02-amazon", "A-JP-SELLER-02")
    await upsert_channel(tenant_b, b_jp, "b-jp-01-amazon", "B-JP-SELLER-01")

    order_a = await upsert_order(
        tenant_a, a_jp, "A-JP-1001", "buyer-jp-001", Decimal("3200"), "SKU-JP-001", "折りたたみ傘"
    )
    order_a2 = await upsert_order(
        tenant_a, a_jp_2, "A2-JP-2001", "buyer-jp-002", Decimal("1800"), "SKU-JP-002", "弁当箱"
    )
    order_b = await upsert_order(
        tenant_b, b_jp, "B-JP-3001", "buyer-jp-101", Decimal("5600"), "SKU-B-001", "寝具セット"
    )
    await upsert_shipment(tenant_a, a_jp, order_a, "YMT-A-0001", True)
    await upsert_shipment(tenant_a, a_jp_2, order_a2, "YMT-A-0002", True)
    await upsert_shipment(tenant_b, b_jp, order_b, "YMT-B-0001", True)
    await upsert_inventory(tenant_a, a_jp, "SKU-JP-001", 42)
    await upsert_inventory(tenant_a, a_jp_2, "SKU-JP-002", 7)
    await upsert_inventory(tenant_b, b_jp, "SKU-B-001", 11)

    sop = (
        await session.execute(
            select(Sop).where(Sop.tenant_id == tenant_a.id, Sop.code == "JP-UNRECEIVED")
        )
    ).scalar_one_or_none()
    if sop is None:
        sop = Sop(tenant_id=tenant_a.id, code="JP-UNRECEIVED", title="日本站未收货与退款处理")
        session.add(sop)
        await session.flush()
        stats["sops"] += 1
    sop_body = (
        "1. 核对工单、买家与订单归属；\n"
        "2. 查询关联运单与承运商轨迹；\n"
        "3. 检索适用的日本站政策与 FAQ；\n"
        "4. 生成区分买家陈述、承运商记录和待核实项的日语草稿；\n"
        "5. 人工审核后发送，并记录渠道回执；\n"
        "6. 退款诉求由具备退款权限的客服人工跟进；\n"
        "7. 记录结论、依据和告知状态后结案。"
    )
    sop_version = (
        await session.execute(
            select(SopVersion).where(SopVersion.sop_id == sop.id, SopVersion.version == 1)
        )
    ).scalar_one_or_none()
    if sop_version is None:
        sop_version = SopVersion(
            tenant_id=tenant_a.id,
            sop_id=sop.id,
            version=1,
            body=sop_body,
            status=KnowledgeStatus.approved,
            checksum=content_hash(sop_body),
            approved_at=_now(),
        )
        session.add(sop_version)
        await session.flush()
    else:
        sop_version.body = sop_body
        sop_version.checksum = content_hash(sop_body)
        sop_version.status = KnowledgeStatus.approved
        sop_version.approved_at = _now()

    async def upsert_doc(
        tenant: Tenant,
        store: Store,
        title: str,
        body: str,
        *,
        language: str = "zh",
        product_scope: str = "*",
    ) -> KnowledgeDoc:
        doc = (
            await session.execute(
                select(KnowledgeDoc).where(
                    KnowledgeDoc.tenant_id == tenant.id,
                    KnowledgeDoc.store_id == store.id,
                    KnowledgeDoc.title == title,
                )
            )
        ).scalar_one_or_none()
        if doc is None:
            doc = KnowledgeDoc(
                tenant_id=tenant.id,
                store_id=store.id,
                marketplace="JP",
                language=language,
                product_scope=product_scope,
                title=title,
                status=KnowledgeStatus.published,
            )
            session.add(doc)
            await session.flush()
            stats["knowledge_docs"] += 1
        version = (
            await session.execute(
                select(KnowledgeVersion).where(
                    KnowledgeVersion.doc_id == doc.id, KnowledgeVersion.version == 1
                )
            )
        ).scalar_one_or_none()
        if version is None:
            version = KnowledgeVersion(
                tenant_id=tenant.id,
                doc_id=doc.id,
                version=1,
                body=body,
                status=KnowledgeStatus.published,
                checksum=content_hash(body),
                approved_at=_now(),
                effective_from=_now() - timedelta(days=1),
            )
            session.add(version)
            await session.flush()
        else:
            version.body = body
            version.checksum = content_hash(body)
            version.status = KnowledgeStatus.published
            version.effective_from = _now() - timedelta(days=1)
        doc.status = KnowledgeStatus.published
        doc.current_version_id = version.id
        await session.execute(
            delete(KnowledgeChunk).where(KnowledgeChunk.version_id == version.id)
        )
        for index, chunk in enumerate(
            [part.strip() for part in body.split("\n") if part.strip()]
        ):
            session.add(
                KnowledgeChunk(
                    tenant_id=tenant.id,
                    version_id=version.id,
                    ordinal=index,
                    text=chunk,
                    embedding=[],
                    meta={"title": title},
                )
            )
        return doc

    await upsert_doc(
        tenant_a,
        a_jp,
        "日本站未收货与退款政策",
        "日本站未收货与退款政策（v1）：\n"
        "先核对订单归属和承运商轨迹。\n"
        "承运商显示妥投但买家未收到时，向承运商发起调查并记录查询编号。\n"
        "调查完成前不得向买家承诺退款。\n"
        "退款由具备退款权限的客服跟进，确认后以日语告知买家。\n"
        "所有外语回复必须经人工审核后发送。",
    )
    await upsert_doc(
        tenant_b,
        b_jp,
        "B 租户专用退款政策",
        "B-only 政策：仅适用于 B 公司日本站。\n"
        "B 公司使用独立退款流程，不得复用到 A 公司。",
    )

    async def upsert_faq(
        tenant: Tenant,
        store: Store,
        question: str,
        answer: str,
        marketplace: str = "JP",
        language: str = "zh",
    ) -> None:
        faq = (
            await session.execute(
                select(Faq).where(
                    Faq.tenant_id == tenant.id,
                    Faq.store_id == store.id,
                    Faq.question == question,
                )
            )
        ).scalar_one_or_none()
        if faq is None:
            session.add(
                Faq(
                    tenant_id=tenant.id,
                    store_id=store.id,
                    marketplace=marketplace,
                    language=language,
                    product_scope="*",
                    question=question,
                    answer=answer,
                    status=KnowledgeStatus.approved,
                    version=1,
                    effective_from=_now() - timedelta(days=1),
                )
            )
            stats["faqs"] += 1
        else:
            faq.answer = answer
            faq.status = KnowledgeStatus.approved
            faq.effective_from = _now() - timedelta(days=1)

    await upsert_faq(
        tenant_a,
        a_jp,
        "日本站物流显示妥投但买家未收到怎么办？",
        "先核对订单与运单，联系承运商调查；调查期间不要承诺退款，由具备退款权限的客服跟进。",
    )
    await upsert_faq(
        tenant_b,
        b_jp,
        "B 公司专用退款口径是什么？",
        "B-only：按 B 公司独立流程处理，禁止跨租户复用。",
    )

    case = (
        await session.execute(
            select(TrainingCase).where(
                TrainingCase.tenant_id == tenant_a.id,
                TrainingCase.title == "日本站未收到包裹标准练习",
            )
        )
    ).scalar_one_or_none()
    if case is None:
        session.add(
            TrainingCase(
                tenant_id=tenant_a.id,
                title="日本站未收到包裹标准练习",
                anonymized_content=(
                    "匿名案例：买家称物流显示妥投但未收到，并提出退款。"
                    "请给出核实步骤、对买家的日语回复要点和退款跟进方式。"
                ),
                sop_version_id=sop_version.id,
                status=TrainingStatus.approved,
                marketplace="JP",
                language="ja",
                product_scope="*",
            )
        )
        stats["training_cases"] += 1

    await session.commit()
    return stats