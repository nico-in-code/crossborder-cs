from __future__ import annotations

import enum
import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class EmployeeRole(str, enum.Enum):
    agent = "agent"
    supervisor = "supervisor"
    knowledge_admin = "knowledge_admin"
    system_admin = "system_admin"


class TicketStatus(str, enum.Enum):
    open = "open"
    awaiting_review = "awaiting_review"
    closed = "closed"


class MessageDirection(str, enum.Enum):
    inbound = "inbound"
    outbound = "outbound"


class TranslationState(str, enum.Enum):
    pending = "pending"
    done = "done"
    failed = "failed"


class ReviewDecision(str, enum.Enum):
    approved = "approved"
    rejected = "rejected"
    superseded = "superseded"


class SendStatus(str, enum.Enum):
    pending = "pending"
    claimed = "claimed"
    submitted = "submitted"
    acked = "acked"
    failed = "failed"
    unknown = "unknown"
    reconciled_sent = "reconciled_sent"
    reconciled_not_sent = "reconciled_not_sent"
    manual = "manual"
    cancelled = "cancelled"


class SendOutcome(str, enum.Enum):
    channel_accepted = "channel_accepted"
    failed = "failed"
    unknown = "unknown"


class FollowupStatus(str, enum.Enum):
    pending = "pending"
    in_progress = "in_progress"
    resolved = "resolved"
    notified = "notified"
    cancelled = "cancelled"


class KnowledgeStatus(str, enum.Enum):
    draft = "draft"
    pending_review = "pending_review"
    approved = "approved"
    published = "published"
    retired = "retired"


class RunStatus(str, enum.Enum):
    running = "running"
    awaiting_review = "awaiting_review"
    completed = "completed"
    failed = "failed"


class ToolCallStatus(str, enum.Enum):
    ok = "ok"
    denied = "denied"
    error = "error"
    timeout = "timeout"


class TrainingStatus(str, enum.Enum):
    draft = "draft"
    approved = "approved"
    retired = "retired"


class UUIDPk:
    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)


class CreatedAt:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Timestamps(CreatedAt):
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class Tenant(Base, UUIDPk):
    __tablename__ = "tenant"

    code: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Store(Base, UUIDPk, CreatedAt):
    __tablename__ = "store"
    __table_args__ = (
        UniqueConstraint("tenant_id", "code", name="uq_store_tenant_code"),
        Index("ix_store_tenant", "tenant_id"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False
    )
    code: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    marketplace: Mapped[str] = mapped_column(String(16), nullable=False)
    domain_id: Mapped[str] = mapped_column(String(32), nullable=False)
    seller_id: Mapped[str] = mapped_column(String(64), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    config: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)


class Employee(Base, UUIDPk, CreatedAt):
    __tablename__ = "employee"
    __table_args__ = (
        UniqueConstraint("email", name="uq_employee_email"),
        Index("ix_employee_tenant_role", "tenant_id", "role"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False
    )
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str] = mapped_column(String(120), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[EmployeeRole] = mapped_column(
        SAEnum(EmployeeRole, native_enum=False, length=32), nullable=False
    )
    refund_authority: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class StoreGrant(Base, UUIDPk, CreatedAt):
    __tablename__ = "store_grant"
    __table_args__ = (
        UniqueConstraint("employee_id", "store_id", name="uq_store_grant_employee_store"),
        Index("ix_store_grant_tenant", "tenant_id"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False
    )
    employee_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("employee.id", ondelete="CASCADE"), nullable=False
    )
    store_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("store.id", ondelete="CASCADE"), nullable=False
    )


class WebSession(Base, UUIDPk, CreatedAt):
    __tablename__ = "web_session"
    __table_args__ = (Index("ix_web_session_employee", "employee_id"),)

    employee_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("employee.id", ondelete="CASCADE"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    csrf_token: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ChannelAccount(Base, UUIDPk, CreatedAt):
    __tablename__ = "channel_account"
    __table_args__ = (
        UniqueConstraint("provider", "external_account_id", name="uq_channel_provider_external"),
        UniqueConstraint("code", name="uq_channel_code"),
        Index("ix_channel_tenant_store", "tenant_id", "store_id"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False
    )
    store_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("store.id", ondelete="CASCADE"), nullable=False
    )
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    external_account_id: Mapped[str] = mapped_column(String(128), nullable=False)
    webhook_secret: Mapped[str] = mapped_column(String(255), nullable=False)
    verified: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class Conversation(Base, UUIDPk, Timestamps):
    __tablename__ = "conversation"
    __table_args__ = (
        UniqueConstraint(
            "channel_account_id", "buyer_external_id", name="uq_conversation_channel_buyer"
        ),
        Index("ix_conversation_tenant_store", "tenant_id", "store_id"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False
    )
    store_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("store.id", ondelete="CASCADE"), nullable=False
    )
    channel_account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("channel_account.id", ondelete="CASCADE"), nullable=False
    )
    buyer_external_id: Mapped[str] = mapped_column(String(128), nullable=False)
    buyer_display_name: Mapped[str | None] = mapped_column(String(120))


class Ticket(Base, UUIDPk, Timestamps):
    __tablename__ = "ticket"
    __table_args__ = (
        UniqueConstraint("tenant_id", "number", name="uq_ticket_tenant_number"),
        Index("ix_ticket_tenant_store_status", "tenant_id", "store_id", "status"),
        Index(
            "uq_ticket_open_conversation",
            "conversation_id",
            unique=True,
            postgresql_where=text("status <> 'closed'"),
        ),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False
    )
    store_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("store.id", ondelete="CASCADE"), nullable=False
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversation.id", ondelete="CASCADE"), nullable=False
    )
    number: Mapped[str] = mapped_column(String(48), nullable=False)
    status: Mapped[TicketStatus] = mapped_column(
        SAEnum(TicketStatus, native_enum=False, length=32),
        default=TicketStatus.open,
        nullable=False,
    )
    priority: Mapped[str] = mapped_column(String(16), default="normal", nullable=False)
    assignee_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("employee.id"))
    intents: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    risk_flags: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    order_candidates: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    summary: Mapped[str | None] = mapped_column(Text)
    next_step: Mapped[str | None] = mapped_column(Text)
    next_owner_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("employee.id"))
    next_follow_up_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class TicketEvent(Base, UUIDPk, CreatedAt):
    __tablename__ = "ticket_event"
    __table_args__ = (Index("ix_ticket_event_ticket", "ticket_id", "created_at"),)

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False
    )
    ticket_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("ticket.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("employee.id"))
    trace_id: Mapped[str | None] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)


class AdminAudit(Base, UUIDPk, CreatedAt):
    __tablename__ = "admin_audit"
    __table_args__ = (Index("ix_admin_audit_tenant", "tenant_id", "created_at"),)

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False
    )
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("employee.id", ondelete="SET NULL")
    )
    target_employee_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("employee.id", ondelete="SET NULL")
    )
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)


class Message(Base, UUIDPk, CreatedAt):
    __tablename__ = "message"
    __table_args__ = (
        UniqueConstraint(
            "channel_account_id", "external_message_id", name="uq_message_channel_external"
        ),
        Index("ix_message_conversation_time", "conversation_id", "created_at"),
        Index("ix_message_tenant_store", "tenant_id", "store_id"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False
    )
    store_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("store.id", ondelete="CASCADE"), nullable=False
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversation.id", ondelete="CASCADE"), nullable=False
    )
    ticket_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("ticket.id", ondelete="SET NULL"))
    channel_account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("channel_account.id", ondelete="CASCADE"), nullable=False
    )
    external_message_id: Mapped[str] = mapped_column(String(128), nullable=False)
    direction: Mapped[MessageDirection] = mapped_column(
        SAEnum(MessageDirection, native_enum=False, length=16),
        default=MessageDirection.inbound,
        nullable=False,
    )
    body_text: Mapped[str] = mapped_column(Text, nullable=False)
    original_language: Mapped[str] = mapped_column(String(16), default="ja", nullable=False)
    translated_text: Mapped[str | None] = mapped_column(Text)
    translated_language: Mapped[str | None] = mapped_column(String(16))
    translation_state: Mapped[TranslationState] = mapped_column(
        SAEnum(TranslationState, native_enum=False, length=16),
        default=TranslationState.pending,
        nullable=False,
    )
    buyer_external_id: Mapped[str] = mapped_column(String(128), nullable=False)
    raw_payload: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    trace_id: Mapped[str] = mapped_column(String(64), nullable=False)


class Draft(Base, UUIDPk, CreatedAt):
    __tablename__ = "draft"
    __table_args__ = (
        UniqueConstraint("ticket_id", "version", name="uq_draft_ticket_version"),
        Index("ix_draft_ticket", "ticket_id", "created_at"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False
    )
    ticket_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("ticket.id", ondelete="CASCADE"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    language: Mapped[str] = mapped_column(String(16), default="ja", nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    structured_facts: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    evidence: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="ready", nullable=False)
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("employee.id"))


class Review(Base, UUIDPk, CreatedAt):
    __tablename__ = "review"
    __table_args__ = (
        Index(
            "uq_review_current_approved",
            "ticket_id",
            unique=True,
            postgresql_where=text("is_current AND decision = 'approved'"),
        ),
        Index("ix_review_draft", "draft_id"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False
    )
    ticket_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("ticket.id", ondelete="CASCADE"), nullable=False
    )
    draft_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("draft.id", ondelete="CASCADE"), nullable=False
    )
    draft_version: Mapped[int] = mapped_column(Integer, nullable=False)
    draft_content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    reviewer_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("employee.id"), nullable=False
    )
    decision: Mapped[ReviewDecision] = mapped_column(
        SAEnum(ReviewDecision, native_enum=False, length=16), nullable=False
    )
    note: Mapped[str | None] = mapped_column(Text)
    is_current: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    decided_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class SendTask(Base, UUIDPk, Timestamps):
    __tablename__ = "send_task"
    __table_args__ = (
        UniqueConstraint("review_id", name="uq_send_task_review"),
        UniqueConstraint("idempotency_key", name="uq_send_task_idempotency"),
        Index("ix_send_task_status_retry", "status", "next_retry_at"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False
    )
    ticket_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("ticket.id", ondelete="CASCADE"), nullable=False
    )
    review_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("review.id", ondelete="CASCADE"), nullable=False
    )
    channel_account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("channel_account.id"), nullable=False
    )
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[SendStatus] = mapped_column(
        SAEnum(SendStatus, native_enum=False, length=32),
        default=SendStatus.pending,
        nullable=False,
    )
    payload: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    next_retry_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)


class SendAttempt(Base, UUIDPk, CreatedAt):
    __tablename__ = "send_attempt"
    __table_args__ = (Index("ix_send_attempt_task", "send_task_id", "created_at"),)

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False
    )
    send_task_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("send_task.id", ondelete="CASCADE"), nullable=False
    )
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    outcome: Mapped[SendOutcome] = mapped_column(
        SAEnum(SendOutcome, native_enum=False, length=24), nullable=False
    )
    external_send_id: Mapped[str | None] = mapped_column(String(128))
    response_code: Mapped[str | None] = mapped_column(String(32))
    error: Mapped[str | None] = mapped_column(Text)
    trace_id: Mapped[str] = mapped_column(String(64), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ChannelReceipt(Base, UUIDPk, CreatedAt):
    __tablename__ = "channel_receipt"
    __table_args__ = (
        UniqueConstraint(
            "channel_account_id", "idempotency_key", name="uq_channel_receipt_account_key"
        ),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False
    )
    channel_account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("channel_account.id", ondelete="CASCADE"), nullable=False
    )
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    external_send_id: Mapped[str] = mapped_column(String(128), nullable=False)
    body_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    accepted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Followup(Base, UUIDPk, Timestamps):
    __tablename__ = "followup"
    __table_args__ = (Index("ix_followup_ticket", "ticket_id", "status"),)

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False
    )
    ticket_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("ticket.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[FollowupStatus] = mapped_column(
        SAEnum(FollowupStatus, native_enum=False, length=24),
        default=FollowupStatus.pending,
        nullable=False,
    )
    owner_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("employee.id"))
    conclusion: Mapped[str | None] = mapped_column(Text)
    basis: Mapped[str | None] = mapped_column(Text)
    notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class KnowledgeDoc(Base, UUIDPk, Timestamps):
    __tablename__ = "knowledge_doc"
    __table_args__ = (
        Index("ix_knowledge_doc_scope", "tenant_id", "store_id", "marketplace", "language"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False
    )
    store_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("store.id"))
    marketplace: Mapped[str] = mapped_column(String(16), nullable=False)
    language: Mapped[str] = mapped_column(String(16), nullable=False)
    product_scope: Mapped[str] = mapped_column(String(64), default="*", nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[KnowledgeStatus] = mapped_column(
        SAEnum(KnowledgeStatus, native_enum=False, length=24),
        default=KnowledgeStatus.draft,
        nullable=False,
    )
    current_version_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))


class KnowledgeVersion(Base, UUIDPk, CreatedAt):
    __tablename__ = "knowledge_version"
    __table_args__ = (
        UniqueConstraint("doc_id", "version", name="uq_knowledge_version_doc_version"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False
    )
    doc_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("knowledge_doc.id", ondelete="CASCADE"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[KnowledgeStatus] = mapped_column(
        SAEnum(KnowledgeStatus, native_enum=False, length=24), nullable=False
    )
    checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    approved_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("employee.id"))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    effective_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    effective_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class KnowledgeChunk(Base, UUIDPk, CreatedAt):
    __tablename__ = "knowledge_chunk"
    __table_args__ = (
        UniqueConstraint("version_id", "ordinal", name="uq_knowledge_chunk_version_ordinal"),
        Index("ix_knowledge_chunk_version", "version_id"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False
    )
    version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("knowledge_version.id", ondelete="CASCADE"), nullable=False
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    meta: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)


class Faq(Base, UUIDPk, Timestamps):
    __tablename__ = "faq"
    __table_args__ = (
        Index("ix_faq_scope", "tenant_id", "store_id", "marketplace", "language"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False
    )
    store_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("store.id"))
    marketplace: Mapped[str] = mapped_column(String(16), nullable=False)
    language: Mapped[str] = mapped_column(String(16), nullable=False)
    product_scope: Mapped[str] = mapped_column(String(64), default="*", nullable=False)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    answer: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[KnowledgeStatus] = mapped_column(
        SAEnum(KnowledgeStatus, native_enum=False, length=24),
        default=KnowledgeStatus.pending_review,
        nullable=False,
    )
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    approved_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("employee.id"))
    effective_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    effective_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Sop(Base, UUIDPk, CreatedAt):
    __tablename__ = "sop"
    __table_args__ = (UniqueConstraint("tenant_id", "code", name="uq_sop_tenant_code"),)

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False
    )
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)


class SopVersion(Base, UUIDPk, CreatedAt):
    __tablename__ = "sop_version"
    __table_args__ = (
        UniqueConstraint("sop_id", "version", name="uq_sop_version_sop_version"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False
    )
    sop_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sop.id", ondelete="CASCADE"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[KnowledgeStatus] = mapped_column(
        SAEnum(KnowledgeStatus, native_enum=False, length=24), nullable=False
    )
    checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    approved_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("employee.id"))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class IndexSnapshot(Base, UUIDPk, CreatedAt):
    __tablename__ = "index_snapshot"
    __table_args__ = (Index("ix_index_snapshot_tenant", "tenant_id", "created_at"),)

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    version_ids: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    collection: Mapped[str] = mapped_column(String(128), nullable=False)
    previous_snapshot_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)


class TrainingCase(Base, UUIDPk, Timestamps):
    __tablename__ = "training_case"
    __table_args__ = (Index("ix_training_case_tenant", "tenant_id", "status"),)

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    anonymized_content: Mapped[str] = mapped_column(Text, nullable=False)
    sop_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sop_version.id"), nullable=False
    )
    status: Mapped[TrainingStatus] = mapped_column(
        SAEnum(TrainingStatus, native_enum=False, length=16),
        default=TrainingStatus.draft,
        nullable=False,
    )
    marketplace: Mapped[str] = mapped_column(String(16), default="JP", nullable=False)
    language: Mapped[str] = mapped_column(String(16), default="ja", nullable=False)
    product_scope: Mapped[str] = mapped_column(String(64), default="*", nullable=False)


class TrainingAttempt(Base, UUIDPk, CreatedAt):
    __tablename__ = "training_attempt"
    __table_args__ = (
        UniqueConstraint(
            "case_id", "employee_id", "revision_no", name="uq_training_attempt_revision"
        ),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False
    )
    case_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("training_case.id", ondelete="CASCADE"), nullable=False
    )
    employee_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("employee.id"), nullable=False
    )
    revision_no: Mapped[int] = mapped_column(Integer, nullable=False)
    answer: Mapped[str] = mapped_column(Text, nullable=False)
    feedback: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    score: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))


class AgentRun(Base, UUIDPk, Timestamps):
    __tablename__ = "agent_run"
    __table_args__ = (
        Index("ix_agent_run_ticket", "ticket_id", "created_at"),
        Index("ix_agent_run_thread", "thread_id"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False
    )
    ticket_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("ticket.id"))
    thread_id: Mapped[str] = mapped_column(String(128), nullable=False)
    graph: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[RunStatus] = mapped_column(
        SAEnum(RunStatus, native_enum=False, length=24),
        default=RunStatus.running,
        nullable=False,
    )
    state_summary: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    trace_id: Mapped[str] = mapped_column(String(64), nullable=False)


class ToolCall(Base, UUIDPk, CreatedAt):
    __tablename__ = "tool_call"
    __table_args__ = (
        UniqueConstraint("tool_call_id", name="uq_tool_call_external_id"),
        Index("ix_tool_call_run", "agent_run_id", "step_no"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False
    )
    agent_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agent_run.id", ondelete="CASCADE"), nullable=False
    )
    tool_call_id: Mapped[str] = mapped_column(String(64), nullable=False)
    tool_name: Mapped[str] = mapped_column(String(128), nullable=False)
    arguments: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    result_status: Mapped[ToolCallStatus] = mapped_column(
        SAEnum(ToolCallStatus, native_enum=False, length=16), nullable=False
    )
    result_summary: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    step_no: Mapped[int] = mapped_column(Integer, nullable=False)
    denied_reason: Mapped[str | None] = mapped_column(String(128))


class Attachment(Base, UUIDPk, CreatedAt):
    __tablename__ = "attachment"
    __table_args__ = (Index("ix_attachment_ticket", "ticket_id"),)

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False
    )
    store_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("store.id", ondelete="CASCADE"), nullable=False
    )
    ticket_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("ticket.id", ondelete="CASCADE"), nullable=False
    )
    object_key: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str] = mapped_column(String(128), nullable=False)
    size: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    access_scope: Mapped[str] = mapped_column(String(32), default="ticket", nullable=False)


class BizOrder(Base, UUIDPk, CreatedAt):
    __tablename__ = "biz_order"
    __table_args__ = (
        UniqueConstraint("tenant_id", "store_id", "order_no", name="uq_biz_order_scope_no"),
        Index("ix_biz_order_buyer", "tenant_id", "store_id", "buyer_external_id"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False
    )
    store_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("store.id", ondelete="CASCADE"), nullable=False
    )
    order_no: Mapped[str] = mapped_column(String(64), nullable=False)
    buyer_external_id: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    total_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(8), default="JPY", nullable=False)
    placed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    items: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    shipping_address: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)


class BizShipment(Base, UUIDPk, CreatedAt):
    __tablename__ = "biz_shipment"
    __table_args__ = (
        UniqueConstraint("tenant_id", "carrier", "tracking_no", name="uq_biz_shipment_tracking"),
        Index("ix_biz_shipment_order", "order_id"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False
    )
    store_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("store.id", ondelete="CASCADE"), nullable=False
    )
    order_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("biz_order.id", ondelete="CASCADE"), nullable=False
    )
    carrier: Mapped[str] = mapped_column(String(64), nullable=False)
    tracking_no: Mapped[str] = mapped_column(String(128), nullable=False)
    delivery_status: Mapped[str] = mapped_column(String(32), nullable=False)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    events: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class BizInventory(Base, UUIDPk, CreatedAt):
    __tablename__ = "biz_inventory"
    __table_args__ = (
        UniqueConstraint("tenant_id", "store_id", "sku", name="uq_biz_inventory_scope_sku"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False
    )
    store_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("store.id", ondelete="CASCADE"), nullable=False
    )
    sku: Mapped[str] = mapped_column(String(64), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )