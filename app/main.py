from __future__ import annotations

import asyncio
import logging
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from sqlalchemy import make_url, text

from app.adapters.channel import FakeChannelAdapter
from app.adapters.commerce import DbCommerceAdapter, ScopeResolver
from app.adapters.ocr import build_ocr_adapter
from app.adapters.vector_index import build_vector_index
from app.api import admin as admin_api
from app.api import attachments as attachments_api
from app.api import auth as auth_api
from app.api import inbound as inbound_api
from app.api import integrations as integrations_api
from app.api import knowledge as knowledge_api
from app.api import tickets as tickets_api
from app.api import training as training_api
from app.api import workbench
from app.config import Settings, get_settings
from app.db import build_engine, build_sessionmaker
from app.services.pipeline import default_adapters
from app.trace import log_event
from app.web import templates
from app.workers.send_worker import send_worker_loop

BASE_DIR = Path(__file__).resolve().parent


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        logging.basicConfig(level=logging.INFO, format="%(message)s")
        async with AsyncExitStack() as stack:
            engine = build_engine(settings)
            app.state.engine = engine
            app.state.sessionmaker = build_sessionmaker(engine)
            readonly_url = settings.mcp_database_url or settings.database_url
            mcp_engine = build_engine(settings, url=readonly_url)
            app.state.mcp_engine = mcp_engine
            app.state.mcp_sessionmaker = build_sessionmaker(mcp_engine)
            app.state.commerce_adapter = DbCommerceAdapter(app.state.mcp_sessionmaker)
            app.state.scope_resolver = ScopeResolver(app.state.mcp_sessionmaker)
            app.state.agent_adapters = default_adapters()
            app.state.channel_adapter = FakeChannelAdapter(app.state.sessionmaker)
            app.state.ocr_adapter = build_ocr_adapter(settings)
            app.state.vector_index = build_vector_index(settings)
            try:
                async with engine.connect() as connection:
                    await connection.execute(text("SELECT 1"))
                checkpoint_dsn = (
                    make_url(settings.database_url)
                    .set(drivername="postgresql")
                    .render_as_string(hide_password=False)
                )
                checkpointer = await stack.enter_async_context(
                    AsyncPostgresSaver.from_conn_string(checkpoint_dsn)
                )
                await checkpointer.setup()
                app.state.checkpointer = checkpointer
            except Exception as exc:
                log_event("database_unavailable", error=type(exc).__name__)
                await engine.dispose()
                await mcp_engine.dispose()
                raise
            stop_event = asyncio.Event()
            worker_task: asyncio.Task | None = None
            if settings.workers_enabled:
                worker_task = asyncio.create_task(
                    send_worker_loop(
                        app.state.sessionmaker,
                        settings,
                        app.state.channel_adapter,
                        stop_event,
                    )
                )
            log_event("app_started", environment=settings.app_env)
            try:
                yield
            finally:
                stop_event.set()
                if worker_task is not None:
                    worker_task.cancel()
                    try:
                        await worker_task
                    except (asyncio.CancelledError, Exception):
                        pass
                await engine.dispose()
                await mcp_engine.dispose()
                log_event("app_stopped")

    app = FastAPI(title="Cross-border CS Workbench", lifespan=lifespan)
    app.state.settings = settings
    app.state.templates = templates

    static_dir = BASE_DIR / "static"
    static_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    app.include_router(inbound_api.router)
    app.include_router(auth_api.router)
    app.include_router(admin_api.router)
    app.include_router(tickets_api.router)
    app.include_router(training_api.router)
    app.include_router(attachments_api.router)
    app.include_router(integrations_api.router)
    app.include_router(knowledge_api.router)
    app.include_router(workbench.router)

    @app.get("/healthz")
    async def healthz(request: Request):
        try:
            async with request.app.state.engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
            return {"status": "ok"}
        except Exception:
            return JSONResponse({"status": "degraded"}, status_code=503)

    return app


app = create_app()