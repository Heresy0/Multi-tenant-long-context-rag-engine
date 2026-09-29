from uuid import uuid4

from langchain_core.embeddings import Embeddings

from backend.app.db.models import EMBEDDING_DIMENSION
from backend.app.pgvector_retriever import PgVectorRetriever
from backend.app.security.retrieval_scope import RetrievalScope
from backend.app.vector_repository import (
    PgVectorRepository,
    VectorSearchHit,
)


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


class FakeRepository(PgVectorRepository):
    def __init__(self, hit: VectorSearchHit) -> None:
        self.hit = hit
        self.calls: list[dict] = []

    def search(
        self,
        *,
        scope: RetrievalScope,
        query_embedding: list[float],
        limit: int,
    ) -> list[VectorSearchHit]:
        self.calls.append({
            "scope": scope,
            "query_embedding": query_embedding,
            "limit": limit,
        })
        return [self.hit]


def test_embeds_query_and_maps_scoped_search_hit() -> None:
    scope = RetrievalScope(
        tenant_id=uuid4(),
        knowledge_base_id=uuid4(),
    )
    document_id = uuid4()
    hit = VectorSearchHit(
        chunk_id="chunk-1",
        document_id=document_id,
        tenant_id=scope.tenant_id,
        knowledge_base_id=scope.knowledge_base_id,
        content="技术手册内容",
        document_name="技术手册.docx",
        source="file:///技术手册.docx",
        metadata={
            "source_id": "source-1",
            "chunk_content_hash": "hash-1",
        },
        distance=0.125,
    )
    embeddings = FakeEmbeddings()
    repository = FakeRepository(hit)
    retriever = PgVectorRetriever(
        repository=repository,
        embeddings=embeddings,
        scope=scope,
        k=12,
    )

    documents = retriever.invoke("  故障怎么处理？  ")

    assert embeddings.queries == ["故障怎么处理？"]
    assert len(repository.calls) == 1
    assert repository.calls[0]["scope"] == scope
    assert repository.calls[0]["limit"] == 12
    assert len(repository.calls[0]["query_embedding"]) == (
        EMBEDDING_DIMENSION
    )
    assert len(documents) == 1
    assert documents[0].id == "chunk-1"
    assert documents[0].page_content == "技术手册内容"
    assert documents[0].metadata["document_id"] == str(
        document_id
    )
    assert documents[0].metadata["vector_distance"] == 0.125
