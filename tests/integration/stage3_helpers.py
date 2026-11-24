from __future__ import annotations

from httpx import AsyncClient
from sqlalchemy import make_url

from app.adapters.channel import FakeChannelAdapter
from app.config import Settings
from app.services.send_queue import process_next_send, process_ready_tasks


def build_test_settings(settings: Settings) -> Settings:
    updates = {
        "database_url": settings.test_database_url,
        "workers_enabled": False,
    }
    if settings.mcp_database_url:
        updates["mcp_database_url"] = (
            make_url(settings.mcp_database_url)
            .set(database=make_url(settings.test_database_url).database)
            .render_as_string(hide_password=False)
        )
    return settings.model_copy(update=updates)


async def approve(
    client: AsyncClient,
    headers: dict,
    ticket_id: str,
    draft_id: str,
    content_hash: str,
    note: str | None = None,
) -> dict:
    response = await client.post(
        f"/api/tickets/{ticket_id}/drafts/{draft_id}/approve",
        headers=headers,
        json={"content_hash": content_hash, "note": note},
    )
    assert response.status_code == 200, response.text
    return response.json()


async def ticket_payload(client: AsyncClient, ticket_id: str) -> dict:
    response = await client.get(f"/api/tickets/{ticket_id}")
    assert response.status_code == 200, response.text
    return response.json()


async def run_send_tick(app, channel=None) -> list[dict]:
    return await process_ready_tasks(
        app.state.sessionmaker,
        app.state.settings,
        channel or app.state.channel_adapter,
    )


async def run_single_send(app, channel=None) -> dict | None:
    return await process_next_send(
        app.state.sessionmaker,
        app.state.settings,
        channel or app.state.channel_adapter,
    )


def flaky_channel(app, mode: str = "accept_then_timeout") -> FakeChannelAdapter:
    return FakeChannelAdapter(app.state.sessionmaker, mode=mode)