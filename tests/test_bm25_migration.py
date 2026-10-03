import importlib
from types import SimpleNamespace

import pytest
from sqlalchemy import Column, MetaData, String, Table, Text, create_engine, select

from backend.app.knowledge.keyword_tokenizer import build_keyword_text

migration = importlib.import_module("migrations.versions.a7c9e1f3b506_add_persistent_bm25")


def test_backfill_batches_preserve_content_and_handle_more_than_one_batch(monkeypatch):
    engine, metadata = create_engine("sqlite://"), MetaData()
    chunks = Table("document_chunks", metadata,
                   Column("id", String(64), primary_key=True), Column("content", Text), Column("keyword_text", Text))
    metadata.create_all(engine)
    monkeypatch.setattr(migration, "BATCH_SIZE", 2)
    with engine.begin() as connection:
        originals = ["访问令牌过期", "RATE-001 429 99.2%", "Idempotency-Key", "！！！", "重复 令牌 令牌"]
        connection.execute(chunks.insert(), [{"id": str(i), "content": text} for i, text in enumerate(originals)])
        assert migration.backfill_keyword_text(connection) == 5
        rows = connection.execute(select(chunks).order_by(chunks.c.id)).mappings().all()
        assert [r["content"] for r in rows] == originals
        assert [r["keyword_text"] for r in rows] == [build_keyword_text(text) for text in originals]
        assert migration.backfill_keyword_text(connection) == 5
    engine.dispose()


def test_offline_migration_fails_before_emitting_incomplete_sql(monkeypatch):
    monkeypatch.setattr(migration.op, "get_context", lambda: SimpleNamespace(as_sql=True))
    with pytest.raises(RuntimeError, match="不支持 --sql"):
        migration.upgrade()


def test_index_uses_whitespace_tokenizer_and_includes_scope_columns():
    assert "keyword_text::pdb.whitespace" in migration.CREATE_INDEX_SQL
    assert "tenant_id, knowledge_base_id, document_id" in migration.CREATE_INDEX_SQL
    assert "key_field='id'" in migration.CREATE_INDEX_SQL
