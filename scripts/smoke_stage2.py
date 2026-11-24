"""One-off smoke run of the stage 2 pipeline against the dev database."""

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app  # noqa: F401

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.adapters.commerce import DbCommerceAdapter, ScopeResolver
from app.config import get_settings
from app.models import Employee, Ticket
from app.services.pipeline import default_adapters, process_ticket


async def main() -> None:
    settings = get_settings()
    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    mcp_engine = create_async_engine(settings.mcp_database_url, pool_pre_ping=True)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    mcp_sessionmaker = async_sessionmaker(mcp_engine, expire_on_commit=False)

    async with sessionmaker() as session:
        employee = await session.scalar(
            select(Employee).where(Employee.email == "cs.a@example.com")
        )
        ticket = await session.scalar(
            select(Ticket).where(Ticket.tenant_id == employee.tenant_id).limit(1)
        )

    result = await process_ticket(
        sessionmaker,
        settings,
        ticket.id,
        employee.id,
        adapters=default_adapters(),
        commerce=DbCommerceAdapter(mcp_sessionmaker),
        resolver=ScopeResolver(mcp_sessionmaker),
    )
    artifact = Path(__file__).resolve().parents[1] / "artifacts" / "stage2-smoke.json"
    artifact.parent.mkdir(exist_ok=True)
    artifact.write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    summary = {
        "ticket_id": result["ticket_id"],
        "route": result["route"],
        "intents": [intent["code"] for intent in result["intents"]],
        "tool_calls": [
            [call["step_no"], call["tool_name"], call["status"]]
            for call in result["tool_calls"]
        ],
        "evidence_count": result["evidence_count"],
        "draft_version": result["draft"]["version"],
        "artifact": str(artifact),
    }
    print(json.dumps(summary, ensure_ascii=True, indent=2))
    await engine.dispose()
    await mcp_engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())