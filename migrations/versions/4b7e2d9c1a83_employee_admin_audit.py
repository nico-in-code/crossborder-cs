"""employee permission administration audit

Revision ID: 4b7e2d9c1a83
Revises: 9f2c41a7b3d5
Create Date: 2026-11-23 20:05:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "4b7e2d9c1a83"
down_revision: Union[str, None] = "9f2c41a7b3d5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "admin_audit",
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("actor_id", sa.Uuid(), nullable=True),
        sa.Column("target_employee_id", sa.Uuid(), nullable=True),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["actor_id"], ["employee.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["target_employee_id"], ["employee.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_admin_audit_tenant", "admin_audit", ["tenant_id", "created_at"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_admin_audit_tenant", table_name="admin_audit")
    op.drop_table("admin_audit")