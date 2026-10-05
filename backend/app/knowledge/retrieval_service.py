from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings
from sqlalchemy.orm import Session

from ..config import Settings
from .hybrid_retriever import (
    create_hybrid_retriever,
)
from .pgvector_retriever import (
    PgVectorRetriever,
)
from .keyword_repository import PgKeywordRepository
from .pg_bm25_retriever import PgBM25Retriever
from .rag import create_embeddings
from .reranker import BaseReranker, QwenReranker
from .rerank_retriever import RerankRetriever
from ..security.retrieval_scope import RetrievalScope
from .vector_repository import PgVectorRepository
from .evidence_selection import select_evidence
from .evidence_expansion import expand_incident_candidates
from uuid import UUID
from ..evaluation.trace import record_documents, record_scope


FINAL_K = 8
FETCH_K = 30


class RetrievalService:
    """在授权范围内执行企业知识库检索。"""

    def __init__(
        self,
        settings: Settings,
        *,
        embeddings: Embeddings | None = None,
        reranker: BaseReranker | None = None,
    ) -> None:
        self._settings = settings
        self._embeddings = (
            embeddings or create_embeddings(settings)
        )
        self._reranker = (
            reranker
            or QwenReranker(
                endpoint=settings.rerank_url,
                api_key=settings.chat_api_key,
                model=settings.rerank_model,
                timeout_seconds=(
                    settings.rerank_timeout_seconds
                ),
            )
        )

    def search(
        self,
        query: str,
        *,
        scope: RetrievalScope,
        session: Session,
    ) -> list[Document]:
        """执行范围内向量、关键词融合和重排序。"""
        query = query.strip()

        if not query:
            raise ValueError("问题不能为空")
        record_scope(query, scope)

        repository = PgVectorRepository(session)

        if not repository.has_ready_chunks(scope=scope):
            for stage in ("vector", "keyword", "fusion", "rerank"):
                record_documents(stage, [])
            return []

        vector_retriever = PgVectorRetriever(
            repository=repository,
            embeddings=self._embeddings,
            scope=scope,
            k=FETCH_K,
        )

        hybrid_retriever = create_hybrid_retriever(
            vector_retriever=vector_retriever,
            keyword_retriever=PgBM25Retriever(
                repository=PgKeywordRepository(session), scope=scope, k=FETCH_K,
            ),
            k=FETCH_K,
            fetch_k=FETCH_K,
        )

        def prepare_candidates(question, candidates):
            candidates = [doc for doc in candidates if scope.contains(
                doc.metadata.get("tenant_id"), doc.metadata.get("knowledge_base_id"))]
            candidates = expand_incident_candidates(question, candidates, repository=repository, scope=scope, budget=FETCH_K)
            record_documents("expanded_candidates", candidates)
            ids = set()
            for doc in candidates:
                try:
                    ids.add(UUID(str(doc.metadata.get("document_id"))))
                except ValueError:
                    continue
            policies = repository.candidate_policies(scope=scope, document_ids=ids) if ids else {}
            for doc in candidates:
                policy = policies.get(str(doc.metadata.get("document_id")))
                if policy:
                    doc.metadata["policy"] = policy
            record_documents("policy_candidates_raw", candidates)
            candidates = select_evidence(question, candidates)
            record_documents("policy_candidates", candidates)
            return candidates

        retriever = RerankRetriever(
            base_retriever=hybrid_retriever,
            reranker=self._reranker,
            top_n=FINAL_K,
            search_kwargs={
                "k": FINAL_K,
                "fetch_k": FETCH_K,
            },
            candidate_processor=prepare_candidates,
            result_processor=select_evidence,
        )

        documents = retriever.invoke(query)
        record_documents("rerank", documents)
        names = {str(kb_id): name for kb_id, name in scope.knowledge_base_names}
        for document in documents:
            document.metadata["knowledge_base_name"] = names.get(document.metadata.get("knowledge_base_id"))
        return documents
