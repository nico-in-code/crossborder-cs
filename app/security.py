from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Any

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError, VerifyMismatchError

_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError):
        return False


def new_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sign_hmac(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def verify_hmac(secret: str, body: bytes, signature: str) -> bool:
    expected = sign_hmac(secret, body)
    return hmac.compare_digest(expected, signature or "")


def create_context_token(secret: str, payload: dict[str, Any], ttl_seconds: int = 900) -> str:
    body = dict(payload)
    body["exp"] = int(time.time()) + ttl_seconds
    raw = json.dumps(body, separators=(",", ":"), sort_keys=True).encode("utf-8")
    signature = hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).digest()
    return f"{base64.urlsafe_b64encode(raw).decode('ascii')}.{base64.urlsafe_b64encode(signature).decode('ascii')}"


def decode_context_token(secret: str, token: str) -> dict[str, Any]:
    try:
        encoded, signature_text = token.split(".", 1)
        raw = base64.urlsafe_b64decode(encoded.encode("ascii"))
        signature = base64.urlsafe_b64decode(signature_text.encode("ascii"))
    except (ValueError, TypeError) as exc:
        raise ValueError("malformed context token") from exc
    expected = hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).digest()
    if not hmac.compare_digest(expected, signature):
        raise ValueError("invalid context signature")
    payload = json.loads(raw.decode("utf-8"))
    if int(payload.get("exp", 0)) < int(time.time()):
        raise ValueError("context token expired")
    return payload