"""Allow all-accessible conversations without broadening existing single-KB ones."""
from alembic import op
import sqlalchemy as sa

revision = "b8d0f2a4c607"
down_revision = "a7c9e1f3b506"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("conversations") as batch:
        batch.alter_column("knowledge_base_id", existing_type=sa.Uuid(), nullable=True)
        batch.create_unique_constraint("uq_conversations_tenant_id", ["tenant_id", "id"])
    with op.batch_alter_table("conversation_turns") as batch:
        batch.alter_column("knowledge_base_id", existing_type=sa.Uuid(), nullable=True)
        batch.add_column(sa.Column("authorized_knowledge_base_ids", sa.JSON(), nullable=False, server_default=sa.text("'[]'")))
        batch.create_foreign_key("fk_conversation_turns_tenant_conversation", "conversations",
                                ["tenant_id", "conversation_id"], ["tenant_id", "id"], ondelete="CASCADE")


def downgrade():
    # 不自动删除新模式会话；先导出/处理它们，避免静默丢失历史。
    if op.get_bind().scalar(sa.text("SELECT count(*) FROM conversations WHERE knowledge_base_id IS NULL")):
        raise RuntimeError("存在全权限范围会话，回滚前请先导出并处理这些会话。")
    with op.batch_alter_table("conversation_turns") as batch:
        batch.drop_constraint("fk_conversation_turns_tenant_conversation", type_="foreignkey")
        batch.drop_column("authorized_knowledge_base_ids")
        batch.alter_column("knowledge_base_id", existing_type=sa.Uuid(), nullable=False)
    with op.batch_alter_table("conversations") as batch:
        batch.drop_constraint("uq_conversations_tenant_id", type_="unique")
        batch.alter_column("knowledge_base_id", existing_type=sa.Uuid(), nullable=False)
