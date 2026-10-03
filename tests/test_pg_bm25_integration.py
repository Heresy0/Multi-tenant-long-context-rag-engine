"""Opt-in tests against a dedicated, already migrated disposable database.

BM25_TEST_DATABASE_URL is intentionally separate from the application DATABASE_URL.
"""
import importlib
import os
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from backend.app.db.models import DocumentChunk, KnowledgeBase, KnowledgeDocument, Tenant
from backend.app.knowledge.keyword_repository import PgKeywordRepository, verify_keyword_index
from backend.app.knowledge.keyword_tokenizer import build_keyword_text
from backend.app.knowledge.pg_bm25_retriever import PgBM25Retriever
from backend.app.security.retrieval_scope import RetrievalScope

pytestmark = pytest.mark.integration


@pytest.fixture
def bm25_session():
    url = os.getenv("BM25_TEST_DATABASE_URL")
    if os.getenv("RUN_BM25_INTEGRATION_TESTS") != "1" or not url:
        pytest.skip("需要独立 BM25_TEST_DATABASE_URL 和 RUN_BM25_INTEGRATION_TESTS=1")
    if not (make_url(url).database or "").endswith("_test"):
        pytest.fail("BM25 集成测试只允许名称以 _test 结尾的专用测试数据库")
    engine = create_engine(url)
    verify_keyword_index(engine)
    connection = engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection, expire_on_commit=False, join_transaction_mode="create_savepoint")
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()
        engine.dispose()


def _scope(session, *, tenant=None):
    tenant = tenant or Tenant(id=uuid4(), name="BM25 测试租户")
    session.add(tenant)
    session.flush()
    kb = KnowledgeBase(id=uuid4(), tenant_id=tenant.id, name=f"BM25 测试知识库 {uuid4().hex}", visibility="restricted")
    session.add(kb)
    session.flush()
    return RetrievalScope(tenant_id=tenant.id, knowledge_base_id=kb.id), tenant


def _document(session, scope, *, status="ready"):
    document = KnowledgeDocument(
        id=uuid4(), tenant_id=scope.tenant_id, knowledge_base_id=scope.knowledge_base_id,
        source_id=uuid4().hex * 2, file_name="测试.md", storage_uri="test://bm25/document",
        mime_type="text/markdown", content_hash=uuid4().hex * 2, status=status, version=1, metadata_json={},
    )
    session.add(document)
    session.flush()
    return document


def _chunk(session, document, content, *, position=0):
    chunk = DocumentChunk(
        id=uuid4().hex * 2, tenant_id=document.tenant_id, knowledge_base_id=document.knowledge_base_id,
        document_id=document.id, chunk_index=position, content=content, keyword_text=build_keyword_text(content),
        content_hash=uuid4().hex * 2, chunking_version="test-v1", embedding_model="test-embedding",
        embedding=[1.0] + [0.0] * 1023, metadata_json={"section_path": ["接口文档"]},
    )
    session.add(chunk)
    session.flush()
    return chunk


@pytest.mark.parametrize("query", ["访问令牌", "RATE-001", "429", "99.2%", "Idempotency-Key"])
def test_real_index_matches_chinese_numbers_and_identifiers(bm25_session, query):
    scope, _ = _scope(bm25_session)
    chunk = _chunk(bm25_session, _document(bm25_session, scope),
                   "访问令牌过期时返回 RATE-001 HTTP 429，99.2% 可用性；Idempotency-Key 是幂等键。")
    _chunk(bm25_session, _document(bm25_session, scope), "人力资源年假说明")
    hits = PgKeywordRepository(bm25_session).search(query, scope=scope)
    assert [hit.chunk_id for hit in hits] == [chunk.id]
    assert hits[0].score > 0
    assert hits[0].content == chunk.content
    documents = PgBM25Retriever(repository=PgKeywordRepository(bm25_session), scope=scope).invoke(query)
    assert documents[0].page_content == chunk.content
    assert documents[0].metadata["keyword_backend"] == "pg_search"


def test_scope_and_ready_filter_apply_before_limit(bm25_session):
    scope, tenant = _scope(bm25_session)
    same_tenant_other_kb, _ = _scope(bm25_session, tenant=tenant)
    other_tenant, _ = _scope(bm25_session)
    visible = _chunk(bm25_session, _document(bm25_session, scope), "隔离标记")
    for excluded_scope in [same_tenant_other_kb, other_tenant]:
        document = _document(bm25_session, excluded_scope)
        for i in range(35):
            _chunk(bm25_session, document, "隔离标记 " * 20, position=i)
    # Older searchable chunks on a non-ready document must not leak either.
    _chunk(bm25_session, _document(bm25_session, scope, status="failed"), "隔离标记 " * 20)
    hits = PgKeywordRepository(bm25_session).search("隔离标记", scope=scope, limit=1)
    assert [hit.chunk_id for hit in hits] == [visible.id]


def test_insert_update_delete_and_rollback_are_visible_atomically(bm25_session):
    scope, _ = _scope(bm25_session)
    chunk = _chunk(bm25_session, _document(bm25_session, scope), "originalmarker")
    repository = PgKeywordRepository(bm25_session)
    with bm25_session.begin_nested() as savepoint:
        chunk.content = "replacementmarker"
        chunk.keyword_text = build_keyword_text(chunk.content)
        bm25_session.flush()
        assert repository.search("originalmarker", scope=scope) == []
        assert [hit.chunk_id for hit in repository.search("replacementmarker", scope=scope)] == [chunk.id]
        savepoint.rollback()
    assert [hit.chunk_id for hit in repository.search("originalmarker", scope=scope)] == [chunk.id]
    assert repository.search("replacementmarker", scope=scope) == []
    bm25_session.delete(chunk)
    bm25_session.flush()
    assert repository.search("originalmarker", scope=scope) == []


def test_blank_and_query_syntax_are_not_sql_or_lucene_commands(bm25_session):
    scope, _ = _scope(bm25_session)
    _chunk(bm25_session, _document(bm25_session, scope), "安全令牌")
    repository = PgKeywordRepository(bm25_session)
    assert repository.search("！！！", scope=scope) == []
    # Match-any operator treats input as token text, not a fielded query language.
    hits = repository.search("安全令牌 OR tenant_id:* '; DROP TABLE document_chunks; --", scope=scope)
    assert len(hits) == 1
    assert bm25_session.scalar(text("SELECT count(*) FROM document_chunks")) >= 1


def test_real_migration_backfills_old_chunks_without_reembedding(bm25_session):
    scope, _ = _scope(bm25_session)
    chunk = _chunk(bm25_session, _document(bm25_session, scope), "历史令牌 RATE-001")
    chunk_id, original = chunk.id, chunk.content
    migration = importlib.import_module("migrations.versions.a7c9e1f3b506_add_persistent_bm25")
    connection = bm25_session.connection()
    # All DDL and data changes roll back with this test's outer transaction.
    with Operations.context(MigrationContext.configure(connection)):
        migration.downgrade()
        migration.upgrade()
    bm25_session.expire_all()
    restored = bm25_session.get(DocumentChunk, chunk_id)
    assert restored.content == original
    assert list(restored.embedding) == [1.0] + [0.0] * 1023
    assert restored.keyword_text == build_keyword_text(original)
    assert [hit.chunk_id for hit in PgKeywordRepository(bm25_session).search("历史令牌", scope=scope)] == [chunk_id]
