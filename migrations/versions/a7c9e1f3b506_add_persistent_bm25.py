"""Persist keyword text and index it with pg_search 0.25.11.

Revision ID: a7c9e1f3b506
Revises: f3a5b7c9d204

Online only: jieba-v1 backfill runs in Python and needs existing row contents.
Stop API/worker writes during this migration; DDL/backfill/index creation is atomic.
"""
from alembic import op
import sqlalchemy as sa

from backend.app.knowledge.keyword_tokenizer import build_keyword_text

revision = "a7c9e1f3b506"
down_revision = "f3a5b7c9d204"
branch_labels = None
depends_on = None
PG_SEARCH_VERSION = "0.25.11"
BATCH_SIZE = 500
INDEX_NAME = "ix_document_chunks_keyword_bm25"
CREATE_INDEX_SQL = f"""
CREATE INDEX {INDEX_NAME} ON document_chunks
USING paradedb (id, (keyword_text::pdb.whitespace), tenant_id, knowledge_base_id, document_id)
WITH (key_field='id')
"""


def backfill_keyword_text(connection) -> int:
    """Keyset batches bound memory; do not re-embed or alter original content."""
    chunks = sa.table("document_chunks", sa.column("id", sa.String(64)),
                      sa.column("content", sa.Text), sa.column("keyword_text", sa.Text))
    last_id, total = None, 0
    update = chunks.update().where(chunks.c.id == sa.bindparam("chunk_id")).values(
        keyword_text=sa.bindparam("tokens"),
    )
    while True:
        statement = sa.select(chunks.c.id, chunks.c.content).order_by(chunks.c.id).limit(BATCH_SIZE)
        if last_id is not None:
            statement = statement.where(chunks.c.id > last_id)
        batch = connection.execute(statement).mappings().all()
        if not batch:
            return total
        connection.execute(update, [{"chunk_id": row["id"], "tokens": build_keyword_text(row["content"])} for row in batch])
        last_id = batch[-1]["id"]
        total += len(batch)


def upgrade() -> None:
    if op.get_context().as_sql:
        raise RuntimeError("BM25 迁移需要在线读取历史正文并执行 jieba 回填，不支持 --sql。")
    connection = op.get_bind()
    preloaded = connection.scalar(sa.text("SHOW shared_preload_libraries"))
    if "pg_search" not in {name.strip() for name in str(preloaded).split(",")}:
        raise RuntimeError("请先启动新数据库镜像，并在 shared_preload_libraries 中预加载 pg_search。")
    available = connection.scalar(sa.text(
        "SELECT default_version FROM pg_available_extensions WHERE name = 'pg_search'"
    ))
    if available != PG_SEARCH_VERSION:
        raise RuntimeError(f"需要安装 pg_search {PG_SEARCH_VERSION}；请先构建 docker/postgres/Dockerfile。")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_search")
    installed = connection.scalar(sa.text("SELECT extversion FROM pg_extension WHERE extname = 'pg_search'"))
    if installed != PG_SEARCH_VERSION:
        raise RuntimeError(f"当前 pg_search 版本不是 {PG_SEARCH_VERSION}，请先验证扩展升级路径。")
    op.add_column("document_chunks", sa.Column("keyword_text", sa.Text(), nullable=True))
    backfill_keyword_text(connection)
    op.alter_column("document_chunks", "keyword_text", nullable=False, server_default=sa.text("''"))
    op.execute(CREATE_INDEX_SQL)


def downgrade() -> None:
    op.drop_index(INDEX_NAME, table_name="document_chunks")
    op.drop_column("document_chunks", "keyword_text")
    # Do not drop a shared extension that other tables may still use.
