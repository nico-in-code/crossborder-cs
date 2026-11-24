# AGENTS.md

## Project

Multi-tenant cross-border e-commerce customer service workbench. Python 3.12 + uv, FastAPI, SQLAlchemy async (psycopg 3), PostgreSQL 16 via SSH tunnel, LangGraph checkpointer, MCP servers, deterministic local adapters.

## Commands

```powershell
uv sync
uv run python scripts/migrate.py          # dev DB
uv run python scripts/seed.py             # demo data
uv run python -m app                      # http://127.0.0.1:8000
uv run pytest -q                          # full suite
uv run pytest tests/unit -q               # fast-ish contract tests
```

Windows: uvicorn must use `--loop app:selector_loop_factory`; `scripts/migrate.py --test` migrates the test DB.

## Rules

- Do not commit or push unless the user explicitly asks. Leave changes local for review.
- Never write remote root passwords, DB passwords, tokens or `.env` contents into the repo, README, logs, plans or test fixtures.
- `.env` stays gitignored; credentials come only from environment/controlled config.
- Real integrations stay disabled unless the store UUID is in `INTEGRATION_ENABLED_STORE_IDS` and `*_ENABLED` is true. Missing prerequisites must raise `NotConfigured`, never silently fall back to fake data.
- Do not touch unrelated remote databases (`recruit`, `resume_lab`); do not expose PostgreSQL 5432 publicly.
- After each stage, run that stage's acceptance before moving on.

## Layout

- `app/adapters/`: Amazon SP-API, pilot channel, 17TRACK, ERP, translation, OCR, vector index, plus local fakes.
- `app/services/`: inbound, draft, review, send queue, diagnosis, training, attachments, index publish.
- `app/mcp/`: business read-only and training read-only servers with signed run context.
- `app/api/`: inbound, auth, tickets, training, attachments, integrations status, knowledge index.
- `docs/contracts/`: integration contracts; `docs/integration-status.md`: real vs local status.

## Tests

- Contract tests use `httpx.MockTransport`: success, 403, 5xx, timeout, and `NotConfigured` when disabled/missing credentials.
- Integration tests require `TEST_DATABASE_URL` whose database name contains `_test`; the suite truncates all tables per test and runs Alembic once per session.
- Attachment tests write under pytest `tmp_path`; do not write test artifacts into `data/attachments/`.