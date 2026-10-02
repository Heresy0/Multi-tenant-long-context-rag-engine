from langchain_core.callbacks import (
    CallbackManagerForRetrieverRun,
)
from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings
from langchain_core.retrievers import BaseRetriever
from pydantic import ConfigDict, Field

from ..security.retrieval_scope import RetrievalScope
from .vector_repository import (
    PgVectorRepository,
    StoredChunk,
    VectorSearchHit,
)


def stored_chunk_to_document(
    chunk: StoredChunk,
) -> Document:
    """将数据库分块转换成LangChain文档。"""
    metadata = dict(chunk.metadata)

    source_id = (
        metadata.get("source_id")
        or str(chunk.document_id)
    )

    metadata.update({
        "source": chunk.source,
        "source_id": str(source_id),
        "document_name": chunk.document_name,
        "document_id": str(chunk.document_id),
        "chunk_id": chunk.chunk_id,
        "tenant_id": str(chunk.tenant_id),
        "knowledge_base_id": str(
            chunk.knowledge_base_id
        ),
    })

    if isinstance(chunk, VectorSearchHit):
        metadata["vector_distance"] = (
            chunk.distance
        )

    return Document(
        id=chunk.chunk_id,
        page_content=chunk.content,
        metadata=metadata,
    )


def vector_search_hit_to_document(
    hit: VectorSearchHit,
) -> Document:
    """将数据库检索结果转换成LangChain文档。"""
    return stored_chunk_to_document(hit)


class PgVectorRetriever(BaseRetriever):
    """在固定授权范围内执行pgvector向量检索。"""

    model_config = ConfigDict(
        arbitrary_types_allowed=True,
    )

    repository: PgVectorRepository
    embeddings: Embeddings
    scope: RetrievalScope
    k: int = Field(
        default=30,
        ge=1,
        le=100,
    )

    def _get_relevant_documents(
        self,
        query: str,
        *,
        run_manager: CallbackManagerForRetrieverRun,
    ) -> list[Document]:
        query = query.strip()

        if not query:
            raise ValueError("检索问题不能为空")

        query_embedding = self.embeddings.embed_query(
            query
        )

        hits = self.repository.search(
            scope=self.scope,
            query_embedding=query_embedding,
            limit=self.k,
        )

        return [
            vector_search_hit_to_document(hit)
            for hit in hits
        ]
