from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app  # noqa: F401  (sets Windows selector loop policy)
from app.config import get_settings
from app.db import build_engine, build_sessionmaker
from app.services.seed import seed_all


async def main() -> None:
    settings = get_settings()
    if not settings.database_url:
        raise SystemExit("DATABASE_URL is not configured")
    engine = build_engine(settings)
    sessionmaker = build_sessionmaker(engine)
    try:
        async with sessionmaker() as session:
            stats = await seed_all(session, settings)
        print(json.dumps({"seeded": stats}, ensure_ascii=False, indent=2))
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())