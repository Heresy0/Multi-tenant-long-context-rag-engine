from uuid import uuid4

from langchain_core.embeddings import Embeddings
import pytest

from backend.app.knowledge import retrieval_service as service_module
from backend.app.db.models import EMBEDDING_DIMENSION
from backend.app.knowledge.reranker import BaseReranker, RerankResult
from backend.app.knowledge.retrieval_service import RetrievalService
from backend.app.security.retrieval_scope import RetrievalScope
from backend.app.knowledge.vector_repository import (
    PgVectorRepository,
    StoredChunk,
    VectorSearchHit,
)
from backend.app.knowledge.keyword_repository import PgKeywordRepository, KeywordSearchHit


class FakeEmbeddings(Embeddings):
    def __init__(self) -> None:
        self.queries: list[str] = []

    def embed_documents(
        self,
        texts: list[str],
    ) -> list[list[float]]:
        return [self._vector() for _text in texts]

    def embed_query(self, text: str) -> list[float]:
        self.queries.append(text)
        return self._vector()

    @staticmethod
    def _vector() -> list[float]:
        return [1.0] + [0.0] * (
            EMBEDDING_DIMENSION - 1
        )


class FakeReranker(BaseReranker):
    calls: list[dict]

    def rerank(
        self,
        *,
        query: str,
        documents: list[str],
        top_n: int,
    ) -> list[RerankResult]:
        self.calls.append({
            "query": query,
            "documents": documents,
            "top_n": top_n,
        })
        return [
            RerankResult(
                index=0,
                relevance_score=0.97,
            )
        ]


class FakeRepository(PgVectorRepository):
    def __init__(
        self,
        chunks: list[StoredChunk],
        hits: list[VectorSearchHit],
    ) -> None:
        self.chunks = chunks
        self.hits = hits
        self.list_scopes: list[RetrievalScope] = []
        self.search_calls: list[dict] = []

    def has_ready_chunks(self, *, scope: RetrievalScope) -> bool:
        self.list_scopes.append(scope)
        return bool(self.chunks)

    def list_chunks(
        self,
        *,
        scope: RetrievalScope,
    ) -> list[StoredChunk]:
        raise AssertionError("正式检索不应加载整个知识库的分块")

    def search(
        self,
        *,
        scope: RetrievalScope,
        query_embedding: list[float],
        limit: int,
    ) -> list[VectorSearchHit]:
        self.search_calls.append({
            "scope": scope,
            "query_embedding": query_embedding,
            "limit": limit,
        })
        return self.hits


def _scope() -> RetrievalScope:
    return RetrievalScope(
        tenant_id=uuid4(),
        knowledge_base_id=uuid4(),
    )


def _stored_chunk(
    scope: RetrievalScope,
) -> StoredChunk:
    return StoredChunk(
        chunk_id="chunk-1",
        document_id=uuid4(),
        tenant_id=scope.tenant_id,
        knowledge_base_id=scope.knowledge_base_id,
        content="RATE-001 表示请求频率超过限制。",
        document_name="开放平台手册.docx",
        source="file:///开放平台手册.docx",
        metadata={
            "source_id": "source-1",
            "chunk_content_hash": "hash-1",
        },
    )


def _search_hit(
    chunk: StoredChunk,
) -> VectorSearchHit:
    return VectorSearchHit(
        chunk_id=chunk.chunk_id,
        document_id=chunk.document_id,
        tenant_id=chunk.tenant_id,
        knowledge_base_id=chunk.knowledge_base_id,
        content=chunk.content,
        document_name=chunk.document_name,
        source=chunk.source,
        metadata=chunk.metadata,
        distance=0.05,
    )


def _service(
    monkeypatch,
    repository: FakeRepository,
) -> tuple[
    RetrievalService,
    FakeEmbeddings,
    FakeReranker,
    list[object],
]:
    embeddings = FakeEmbeddings()
    reranker = FakeReranker(calls=[])
    sessions: list[object] = []

    def create_repository(session: object) -> FakeRepository:
        sessions.append(session)
        return repository

    monkeypatch.setattr(
        service_module,
        "PgVectorRepository",
        create_repository,
    )

    class FakeKeywords(PgKeywordRepository):
        def search(self, query, *, scope, limit=30):
            assert limit == 30
            return [KeywordSearchHit(
                chunk_id=c.chunk_id, document_id=c.document_id, tenant_id=c.tenant_id,
                knowledge_base_id=c.knowledge_base_id, content=c.content,
                document_name=c.document_name, source=c.source, metadata=c.metadata, score=1.5,
            ) for c in repository.chunks]

    monkeypatch.setattr(service_module, "PgKeywordRepository", FakeKeywords)

    service = RetrievalService(
        settings=object(),
        embeddings=embeddings,
        reranker=reranker,
    )
    return service, embeddings, reranker, sessions


def test_rejects_blank_query_before_database_access(
    monkeypatch,
) -> None:
    repository = FakeRepository([], [])
    service, embeddings, reranker, sessions = _service(
        monkeypatch,
        repository,
    )

    with pytest.raises(ValueError, match="问题不能为空"):
        service.search(
            "   ",
            scope=_scope(),
            session=object(),
        )

    assert sessions == []
    assert embeddings.queries == []
    assert reranker.calls == []


def test_empty_scope_returns_without_embedding_or_reranking(
    monkeypatch,
) -> None:
    scope = _scope()
    session = object()
    repository = FakeRepository([], [])
    service, embeddings, reranker, sessions = _service(
        monkeypatch,
        repository,
    )

    results = service.search(
        "没有资料的问题",
        scope=scope,
        session=session,
    )

    assert results == []
    assert sessions == [session]
    assert repository.list_scopes == [scope]
    assert repository.search_calls == []
    assert embeddings.queries == []
    assert reranker.calls == []


def test_search_runs_scoped_pgvector_hybrid_and_rerank_pipeline(
    monkeypatch,
) -> None:
    scope = _scope()
    session = object()
    chunk = _stored_chunk(scope)
    repository = FakeRepository(
        [chunk],
        [_search_hit(chunk)],
    )
    service, embeddings, reranker, sessions = _service(
        monkeypatch,
        repository,
    )

    results = service.search(
        "  RATE-001 是什么？  ",
        scope=scope,
        session=session,
    )

    assert sessions == [session]
    assert repository.list_scopes == [scope]
    assert embeddings.queries == ["RATE-001 是什么？"]
    assert len(repository.search_calls) == 1
    assert repository.search_calls[0]["scope"] == scope
    assert repository.search_calls[0]["limit"] == 30
    assert reranker.calls == [{
        "query": "RATE-001 是什么？",
        "documents": [chunk.content],
        "top_n": 8,
    }]
    assert len(results) == 1
    assert results[0].id == chunk.chunk_id
    assert results[0].metadata["rerank_status"] == "success"
    assert results[0].metadata["rerank_score"] == 0.97
    assert results[0].metadata["tenant_id"] == str(
        scope.tenant_id
    )


def test_multi_scope_shares_budget_and_drops_out_of_scope_hits_before_reranking(monkeypatch):
    tenant, kb1, kb2 = uuid4(), uuid4(), uuid4()
    scope = RetrievalScope(tenant, knowledge_base_ids={kb1, kb2}, knowledge_base_names=((kb1, "公共"), (kb2, "技术")))
    from dataclasses import replace
    first = _stored_chunk(RetrievalScope(tenant, kb1))
    second = replace(_stored_chunk(RetrievalScope(tenant, kb2)), chunk_id="two", content="第二库内容", metadata={"source_id": "two", "chunk_content_hash": "two"})
    denied = replace(_stored_chunk(RetrievalScope(tenant, uuid4())), chunk_id="denied", content="不可发送的敏感内容")
    foreign = replace(_stored_chunk(RetrievalScope(uuid4(), uuid4())), chunk_id="foreign", content="跨租户敏感内容")
    chunks = [first, second, denied, foreign]
    repository = FakeRepository(chunks, [_search_hit(c) for c in chunks])
    service, embeddings, reranker, _ = _service(monkeypatch, repository)
    results = service.search("跨库问题", scope=scope, session=object())
    assert len(embeddings.queries) == 1
    assert len(repository.search_calls) == 1
    assert repository.search_calls[0]["limit"] == 30
    assert set(reranker.calls[0]["documents"]) == {first.content, second.content}
    assert reranker.calls[0]["top_n"] == 8
    assert results[0].metadata["knowledge_base_name"] in {"公共", "技术"}
