from langchain_core.callbacks import CallbackManagerForRetrieverRun
from langchain_core.documents import Document
from langchain_core.retrievers import BaseRetriever
from pydantic import ConfigDict, Field

from ..security.retrieval_scope import RetrievalScope
from .keyword_repository import PgKeywordRepository
from .pgvector_retriever import stored_chunk_to_document


class PgBM25Retriever(BaseRetriever):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    repository: PgKeywordRepository
    scope: RetrievalScope
    k: int = Field(default=30, ge=1, le=100)

    def _get_relevant_documents(
        self, query: str, *, run_manager: CallbackManagerForRetrieverRun,
    ) -> list[Document]:
        documents = []
        for hit in self.repository.search(query, scope=self.scope, limit=self.k):
            if hit.tenant_id != self.scope.tenant_id or hit.knowledge_base_id not in self.scope.knowledge_base_ids:
                continue
            document = stored_chunk_to_document(hit)
            document.metadata.update(bm25_score=hit.score, keyword_backend="pg_search")
            documents.append(document)
        return documents
