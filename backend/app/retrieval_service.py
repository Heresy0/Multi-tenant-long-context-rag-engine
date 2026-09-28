from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings
from sqlalchemy.orm import Session

from .config import Settings
from .hybrid_retriever import (
    create_hybrid_retriever,
)
from .pgvector_retriever import (
    PgVectorRetriever,
    stored_chunk_to_document,
)
from .rag import create_embeddings
from .reranker import BaseReranker, QwenReranker
from .rerank_retriever import RerankRetriever
from .security.retrieval_scope import RetrievalScope
from .vector_repository import PgVectorRepository


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

        repository = PgVectorRepository(session)

        stored_chunks = repository.list_chunks(
            scope=scope
        )

        if not stored_chunks:
            return []

        keyword_documents = [
            stored_chunk_to_document(chunk)
            for chunk in stored_chunks
        ]

        vector_retriever = PgVectorRetriever(
            repository=repository,
            embeddings=self._embeddings,
            scope=scope,
            k=FETCH_K,
        )

        hybrid_retriever = create_hybrid_retriever(
            vector_retriever=vector_retriever,
            keyword_documents=keyword_documents,
            k=FETCH_K,
            fetch_k=FETCH_K,
        )

        retriever = RerankRetriever(
            base_retriever=hybrid_retriever,
            reranker=self._reranker,
            top_n=FINAL_K,
            search_kwargs={
                "k": FINAL_K,
                "fetch_k": FETCH_K,
            },
        )

        return retriever.invoke(query)