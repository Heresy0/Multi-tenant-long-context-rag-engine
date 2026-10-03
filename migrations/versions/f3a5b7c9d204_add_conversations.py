"""Add private knowledge-base-scoped conversations.

Revision ID: f3a5b7c9d204
Revises: d2f4a6c8e103
"""
from alembic import op
import sqlalchemy as sa

revision = "f3a5b7c9d204"
down_revision = "d2f4a6c8e103"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "conversations",
        sa.Column("id", sa.Uuid(), nullable=False, primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("knowledge_base_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("turn_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("lease_token", sa.String(32)),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id", "user_id"], ["users.tenant_id", "users.id"], name="fk_conversations_user", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id", "knowledge_base_id"], ["knowledge_bases.tenant_id", "knowledge_bases.id"], name="fk_conversations_kb", ondelete="CASCADE"),
        sa.UniqueConstraint("tenant_id", "knowledge_base_id", "id", name="uq_conversations_scope_id"),
        sa.CheckConstraint("turn_count >= 0", name="ck_conversations_turn_count"),
    )
    op.create_index("ix_conversations_owner_updated", "conversations", ["tenant_id", "user_id", "knowledge_base_id", "updated_at"])
    op.create_table(
        "conversation_turns",
        sa.Column("id", sa.Uuid(), nullable=False, primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("knowledge_base_id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("turn_index", sa.Integer(), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("retrieval_question", sa.Text(), nullable=False),
        sa.Column("result_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id", "knowledge_base_id", "conversation_id"], ["conversations.tenant_id", "conversations.knowledge_base_id", "conversations.id"], name="fk_conversation_turns_conversation", ondelete="CASCADE"),
        sa.UniqueConstraint("conversation_id", "turn_index", name="uq_conversation_turns_position"),
        sa.CheckConstraint("turn_index >= 1", name="ck_conversation_turns_index"),
    )


def downgrade() -> None:
    op.drop_table("conversation_turns")
    op.drop_index("ix_conversations_owner_updated", table_name="conversations")
    op.drop_table("conversations")
