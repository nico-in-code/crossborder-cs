from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import app  # noqa: F401  (sets Windows selector loop policy)
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import make_url, text

from app.config import Settings
from app.db import build_engine, build_sessionmaker
from app.main import create_app
from app.models import Base
from app.services.seed import seed_all

ALL_TABLES = tuple(
    name for name in Base.metadata.tables if name != "alembic_version"
)


@pytest.fixture(scope="session")
def settings() -> Settings:
    current = Settings()
    if not current.test_database_url:
        pytest.skip("TEST_DATABASE_URL is not configured")
    database_name = make_url(current.test_database_url).database or ""
    if "_test" not in database_name:
        raise RuntimeError(
            "refusing to run tests against a database whose name lacks _test"
        )
    return current


@pytest.fixture(scope="session", autouse=True)
def migrated(settings: Settings) -> None:
    command = [
        sys.executable,
        "-m",
        "alembic",
        "-x",
        f"db_url={settings.test_database_url}",
        "upgrade",
        "head",
    ]
    result = subprocess.run(
        command, cwd=ROOT, capture_output=True, text=True, timeout=180
    )
    if result.returncode != 0:
        raise RuntimeError(f"alembic upgrade head failed: {result.stderr[-2000:]}")


@pytest_asyncio.fixture(scope="session")
async def test_engine(settings: Settings, migrated: None):
    engine = build_engine(settings, url=settings.test_database_url)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture(scope="session")
async def test_sessionmaker(test_engine):
    return build_sessionmaker(test_engine)


@pytest_asyncio.fixture(autouse=True)
async def clean_db(test_engine):
    async with test_engine.begin() as connection:
        names = ", ".join(f'"{name}"' for name in ALL_TABLES)
        await connection.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))
    yield


@pytest_asyncio.fixture
async def seeded(test_sessionmaker, settings: Settings):
    async with test_sessionmaker() as session:
        await seed_all(session, settings)


@pytest_asyncio.fixture
async def app_instance(settings: Settings):
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
    test_settings = settings.model_copy(update=updates)
    application = create_app(test_settings)
    async with application.router.lifespan_context(application):
        yield application


@pytest_asyncio.fixture
async def client(app_instance):
    transport = ASGITransport(app=app_instance)
    async with AsyncClient(
        transport=transport, base_url="http://testserver", timeout=30
    ) as http_client:
        yield http_client


@pytest_asyncio.fixture
async def db(test_sessionmaker):
    async with test_sessionmaker() as session:
        yield session


@pytest.fixture
def inbound_sender(settings: Settings):
    from app.security import sign_hmac

    async def _send(client: AsyncClient, account_code: str, payload: dict):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        secret = f"{settings.test_channel_secret}-{account_code}"
        return await client.post(
            f"/api/inbound/{account_code}",
            content=body,
            headers={
                "content-type": "application/json",
                "X-Channel-Signature": sign_hmac(secret, body),
            },
        )

    return _send


@pytest.fixture
def api_login(settings: Settings):
    async def _login(client: AsyncClient, email: str, password: str | None = None):
        response = await client.post(
            "/api/auth/login",
            json={"email": email, "password": password or settings.seed_password},
        )
        assert response.status_code == 200, response.text
        return response.json()

    return _login