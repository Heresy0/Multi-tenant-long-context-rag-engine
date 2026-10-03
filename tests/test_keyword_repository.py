from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql

from backend.app.db.models import DocumentChunk, KnowledgeDocument
from backend.app.knowledge.keyword_repository import PgKeywordRepository, KeywordSearchHit, verify_keyword_index
from backend.app.knowledge.keyword_tokenizer import build_keyword_text, tokenize_chinese
from backend.app.knowledge.pg_bm25_retriever import PgBM25Retriever
from backend.app.security.retrieval_scope import RetrievalScope


class FakeSession:
    def __init__(self, rows=()):
        self.rows, self.calls = rows, []

    def execute(self, statement, parameters):
        self.calls.append((statement, parameters))
        return SimpleNamespace(all=lambda: self.rows)


def scope():
    return RetrievalScope(uuid4(), uuid4())


def test_shared_tokenizer_preserves_chinese_identifiers_numbers_and_repeats():
    tokens = tokenize_chinese("令牌 RATE-001 Idempotency-Key 99.2% 429 令牌")
    assert "令牌" in tokens
    assert tokens.count("令牌") == 2
    assert {"rate-001", "idempotency-key", "99.2%", "429"} <= set(tokens)
    assert build_keyword_text("令牌 RATE-001") == " ".join(tokenize_chinese("令牌 RATE-001"))


@pytest.mark.parametrize("query", ["", "  ", "！！！", "?!"])
def test_empty_tokens_do_not_query_database(query):
    session = FakeSession()
    assert PgKeywordRepository(session).search(query, scope=scope()) == []
    assert session.calls == []


@pytest.mark.parametrize("limit", [0, -1, 101])
def test_invalid_limits_are_rejected(limit):
    session = FakeSession()
    with pytest.raises(ValueError):
        PgKeywordRepository(session).search("问题", scope=scope(), limit=limit)
    assert session.calls == []


def test_query_is_parameterized_and_scope_and_ready_filter_precede_limit():
    session, allowed = FakeSession(), scope()
    malicious = "RATE-001 ' OR 1=1 -- ; DROP TABLE documents;"
    assert PgKeywordRepository(session).search(malicious, scope=allowed, limit=7) == []
    statement, parameters = session.calls[0]
    compiled = str(statement.compile(dialect=postgresql.dialect()))
    assert "DROP TABLE" not in compiled
    assert "|||" in compiled
    assert "pdb.score(document_chunks.id)" in compiled
    assert "document_chunks.tenant_id = %(tenant_id)s" in compiled
    assert "document_chunks.knowledge_base_id = %(knowledge_base_id)s" in compiled
    assert "documents.status =" in compiled
    assert compiled.index("WHERE") < compiled.index("ORDER BY") < compiled.index("LIMIT")
    assert parameters == {"tenant_id": allowed.tenant_id, "knowledge_base_id": allowed.knowledge_base_id,
                          "keyword_query": build_keyword_text(malicious), "result_limit": 7}


def test_results_preserve_original_content_and_citation_metadata():
    allowed = scope()
    document = KnowledgeDocument(id=uuid4(), tenant_id=allowed.tenant_id,
        knowledge_base_id=allowed.knowledge_base_id, file_name="平台手册.pdf", storage_uri="file:///manual.pdf", source_id="source")
    chunk = DocumentChunk(id="c" * 64, tenant_id=allowed.tenant_id,
        knowledge_base_id=allowed.knowledge_base_id, document_id=document.id,
        content="RATE-001 表示超过频率限制。", chunk_index=0, content_hash="hash",
        metadata_json={"section_path": "接口 / 限流"})
    repository = PgKeywordRepository(FakeSession([(chunk, document, 2.7)]))
    results = PgBM25Retriever(repository=repository, scope=allowed, k=3).invoke("RATE-001")
    assert results[0].page_content == chunk.content
    assert results[0].id == chunk.id
    assert results[0].metadata["bm25_score"] == 2.7
    assert results[0].metadata["keyword_backend"] == "pg_search"
    assert results[0].metadata["document_id"] == str(document.id)
    assert results[0].metadata["section_path"] == "接口 / 限流"


@pytest.mark.parametrize("version,index,preload,message", [
    (None, True, "pg_search", "pg_search 0.25.11"),
    ("0.24.0", True, "pg_search", "pg_search 0.25.11"),
    ("0.25.11", False, "pg_search", "索引未就绪"),
    ("0.25.11", True, "", "预加载"),
    ("0.25.11", True, "pg_stat_statements, pg_search", None),
])
def test_startup_checks_require_pinned_extension_valid_index_and_preload(version, index, preload, message):
    class Connection:
        def __enter__(self):
            self.values = iter([version, index, preload]); return self
        def __exit__(self, *_):
            pass
        def scalar(self, *_):
            return next(self.values)
    engine = SimpleNamespace(connect=lambda: Connection())
    if message:
        with pytest.raises(RuntimeError, match=message):
            verify_keyword_index(engine)
    else:
        verify_keyword_index(engine)
