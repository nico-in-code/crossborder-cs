from __future__ import annotations

import asyncio

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.adapters.channel import ChannelAdapter
from app.config import Settings
from app.services.send_queue import process_ready_tasks
from app.trace import log_event


async def send_worker_loop(
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    channel: ChannelAdapter,
    stop_event: asyncio.Event,
    interval: float = 1.0,
) -> None:
    while not stop_event.is_set():
        try:
            results = await process_ready_tasks(
                sessionmaker, settings, channel, max_tasks=10
            )
            for result in results:
                log_event("send_worker_result", **result)
        except Exception as exc:  # noqa: BLE001 - worker must keep running
            log_event("send_worker_error", error=type(exc).__name__)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except TimeoutError:
            continue