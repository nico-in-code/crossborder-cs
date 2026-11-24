"""grant mcp read-only role access to business and training tables

Revision ID: 9f2c41a7b3d5
Revises: 7b23f93230fb
Create Date: 2026-11-23 10:12:00.000000

"""
from typing import Sequence, Union

from alembic import op

revision: str = "9f2c41a7b3d5"
down_revision: Union[str, None] = "7b23f93230fb"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

MCP_TABLES = (
    "biz_inventory",
    "biz_order",
    "biz_shipment",
    "sop",
    "sop_version",
    "training_case",
)


def upgrade() -> None:
    tables = ", ".join(MCP_TABLES)
    op.execute(
        f"""
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'crossborder_cs_ro') THEN
    GRANT SELECT ON {tables} TO crossborder_cs_ro;
  END IF;
END
$$;
"""
    )


def downgrade() -> None:
    # SELECT grants are additive and harmless to retain.
    pass