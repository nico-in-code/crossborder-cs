from __future__ import annotations

import uuid
from dataclasses import dataclass


class NotConfigured(RuntimeError):
    """The integration is disabled or missing credentials."""


class PermissionDenied(RuntimeError):
    """The upstream rejected the credentials or the requested resource scope."""


class UpstreamError(RuntimeError):
    """The upstream returned a server error."""


class UpstreamTimeout(RuntimeError):
    """The upstream did not answer within the configured timeout."""


def parse_store_ids(raw: str) -> frozenset[str]:
    return frozenset(
        part.strip().lower() for part in (raw or "").split(",") if part.strip()
    )


@dataclass(frozen=True)
class StoreEnablement:
    """Per-store switch for real integrations; empty means all stores disabled."""

    store_ids: frozenset[str]

    @classmethod
    def from_settings(cls, raw: str) -> "StoreEnablement":
        return cls(parse_store_ids(raw))

    def is_enabled(self, store_id: uuid.UUID | str) -> bool:
        return str(store_id).lower() in self.store_ids

    def ensure(self, store_ids: set[uuid.UUID] | list[uuid.UUID]) -> uuid.UUID:
        for store_id in store_ids:
            if self.is_enabled(store_id):
                return store_id
        raise NotConfigured("no enabled store matched the request scope")


def ensure_ready(
    *,
    name: str,
    flag: bool,
    credentials: dict[str, str],
    store_ids: set[uuid.UUID] | list[uuid.UUID],
    enablement: StoreEnablement,
) -> uuid.UUID:
    if not flag:
        raise NotConfigured(f"{name} integration is disabled")
    missing = [key for key, value in credentials.items() if not value]
    if missing:
        raise NotConfigured(f"{name} integration is missing credentials: {', '.join(missing)}")
    return enablement.ensure(store_ids)