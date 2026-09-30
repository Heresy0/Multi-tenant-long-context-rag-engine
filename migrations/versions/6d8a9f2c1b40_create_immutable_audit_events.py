"""create immutable audit events

Revision ID: 6d8a9f2c1b40
Revises: c4a1d7e2f903
Create Date: 2026-10-01

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "6d8a9f2c1b40"
down_revision: Union[str, Sequence[str], None] = (
    "c4a1d7e2f903"
)
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "audit_events",
        sa.Column(
            "id",
            sa.Uuid(),
            nullable=False,
        ),
        sa.Column(
            "tenant_id",
            sa.Uuid(),
            nullable=False,
        ),
        sa.Column(
            "actor_user_id",
            sa.Uuid(),
            nullable=True,
        ),
        sa.Column(
            "knowledge_base_id",
            sa.Uuid(),
            nullable=True,
        ),
        sa.Column(
            "action",
            sa.String(length=100),
            nullable=False,
        ),
        sa.Column(
            "resource_type",
            sa.String(length=50),
            nullable=False,
        ),
        sa.Column(
            "resource_id",
            sa.String(length=255),
            nullable=True,
        ),
        sa.Column(
            "outcome",
            sa.String(length=20),
            nullable=False,
        ),
        sa.Column(
            "request_id",
            sa.String(length=64),
            nullable=True,
        ),
        sa.Column(
            "details_json",
            sa.JSON(),
            nullable=False,
        ),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "outcome IN ('success', 'denied', 'invalid', 'failed')",
            name="ck_audit_events_outcome",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_audit_events_tenant_occurred",
        "audit_events",
        ["tenant_id", "occurred_at"],
    )
    op.create_index(
        "ix_audit_events_actor_occurred",
        "audit_events",
        ["tenant_id", "actor_user_id", "occurred_at"],
    )
    op.create_index(
        "ix_audit_events_kb_occurred",
        "audit_events",
        ["tenant_id", "knowledge_base_id", "occurred_at"],
    )
    op.create_index(
        "ix_audit_events_action_occurred",
        "audit_events",
        ["tenant_id", "action", "occurred_at"],
    )

    op.execute(
        """
        CREATE FUNCTION reject_audit_event_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION
                'audit_events is append-only'
                USING ERRCODE = '55000';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER audit_events_append_only
        BEFORE UPDATE OR DELETE ON audit_events
        FOR EACH ROW
        EXECUTE FUNCTION reject_audit_event_mutation()
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS audit_events_append_only "
        "ON audit_events"
    )
    op.execute(
        "DROP FUNCTION IF EXISTS "
        "reject_audit_event_mutation()"
    )
    op.drop_index(
        "ix_audit_events_action_occurred",
        table_name="audit_events",
    )
    op.drop_index(
        "ix_audit_events_kb_occurred",
        table_name="audit_events",
    )
    op.drop_index(
        "ix_audit_events_actor_occurred",
        table_name="audit_events",
    )
    op.drop_index(
        "ix_audit_events_tenant_occurred",
        table_name="audit_events",
    )
    op.drop_table("audit_events")
