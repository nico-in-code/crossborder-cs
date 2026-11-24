from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.models import EmployeeRole


class InboundPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    external_message_id: str = Field(min_length=1, max_length=128)
    buyer_external_id: str = Field(min_length=1, max_length=128)
    buyer_display_name: str | None = None
    text: str = Field(min_length=1)
    occurred_at: datetime | None = None
    order_candidates: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class LoginPayload(BaseModel):
    email: str
    password: str


class ApprovalPayload(BaseModel):
    draft_id: str
    draft_version: int
    draft_content_hash: str
    note: str | None = None


class EditDraftPayload(BaseModel):
    body: str
    note: str | None = None


class ApprovePayload(BaseModel):
    content_hash: str
    note: str | None = None


class RejectPayload(BaseModel):
    note: str | None = None


class AssignPayload(BaseModel):
    assignee_id: str


class FollowupPayload(BaseModel):
    conclusion: str
    basis: str
    notified: bool = False


class ClosePayload(BaseModel):
    conclusion: str
    basis: str
    notified: bool


class DiagnoseApplyPayload(BaseModel):
    diagnosis_id: str | None = None
    next_step: str
    next_owner_id: str | None = None
    next_follow_up_at: datetime | None = None
    note: str | None = None


class TrainingAttemptPayload(BaseModel):
    answer: str = Field(min_length=1)


class IndexPublishPayload(BaseModel):
    version_ids: list[str] = Field(min_length=1)
    collection: str = "knowledge_chunks"


class IndexRollbackPayload(BaseModel):
    snapshot_id: str


class EmployeeUpdatePayload(BaseModel):
    role: EmployeeRole | None = None
    refund_authority: bool | None = None
    active: bool | None = None


class StoreGrantPayload(BaseModel):
    store_id: str