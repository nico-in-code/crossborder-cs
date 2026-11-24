from __future__ import annotations

import pytest

from app.models import Employee, EmployeeRole
from app.security import (
    content_hash,
    decode_context_token,
    create_context_token,
    hash_password,
    sign_hmac,
    verify_hmac,
    verify_password,
)
from app.services.permission import require_role


@pytest.mark.unit
def test_hmac_rejects_tampered_body():
    body = '{"text":"こんにちは"}'.encode()
    signature = sign_hmac("secret", body)
    assert verify_hmac("secret", body, signature)
    assert not verify_hmac("secret", body + b" ", signature)
    assert not verify_hmac("other-secret", body, signature)


@pytest.mark.unit
def test_password_hash_roundtrip():
    hashed = hash_password("DevPass#2026")
    assert hashed != "DevPass#2026"
    assert verify_password("DevPass#2026", hashed)
    assert not verify_password("wrong", hashed)


@pytest.mark.unit
def test_context_token_is_signed_and_expires():
    token = create_context_token(
        "context-secret",
        {"tenant_id": "t1", "store_id": "s1", "employee_id": "e1"},
        ttl_seconds=60,
    )
    payload = decode_context_token("context-secret", token)
    assert payload["tenant_id"] == "t1"
    with pytest.raises(ValueError):
        decode_context_token("other-secret", token)

    expired = create_context_token("context-secret", {"tenant_id": "t1"}, ttl_seconds=-1)
    with pytest.raises(ValueError):
        decode_context_token("context-secret", expired)


@pytest.mark.unit
def test_content_hash_changes_with_content():
    assert content_hash("abc") == content_hash("abc")
    assert content_hash("abc") != content_hash("abd")


@pytest.mark.unit
def test_require_role_denies_other_roles():
    employee = Employee(
        tenant_id=None,
        email="kb@example.com",
        display_name="KB",
        password_hash="x",
        role=EmployeeRole.knowledge_admin,
        refund_authority=False,
        active=True,
    )
    require_role(employee, EmployeeRole.knowledge_admin)
    with pytest.raises(Exception) as excinfo:
        require_role(employee, EmployeeRole.agent)
    assert getattr(excinfo.value, "status_code", None) == 403