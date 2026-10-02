import logging
import time

from langchain_core.callbacks import (
    CallbackManagerForRetrieverRun,
)
from langchain_core.documents import Document
from langchain_core.retrievers import BaseRetriever
from pydantic import Field

from .reranker import BaseReranker, RerankError


logger = logging.getLogger(__name__)


def copy_document(
        document: Document,
        **extra_metadata,
) -> Document:
    metadata = dict(document.metadata)
    metadata.update(extra_metadata)

    kwargs = {
        "page_content": document.page_content,
        "metadata": metadata,
    }

    document_id = getattr(document,"id",None)

    if document_id is not None:
        kwargs["id"] = document_id

    return Document(**kwargs)


class RerankRetriever(BaseRetriever):
    base_retriever: BaseRetriever
    reranker: BaseReranker
    top_n: int = 8

    search_type: str = "hybrid_rrf_rerank"
    search_kwargs: dict[str, int] = Field(
        default_factory=lambda:{
            "k":8,
            "fetch_k":30,
        }
    )

    def _get_relevant_documents(
        self,
        query: str,
        *,
        run_manager:CallbackManagerForRetrieverRun,
    ) -> list[Document]:
        retrieval_started = time.perf_counter()
        candidates = self.base_retriever.invoke(query)
        retrieval_latency_ms = round(
            (time.perf_counter() - retrieval_started) * 1000,
            2,
        )

        if not candidates:
            return []

        rerank_started = time.perf_counter()

        try:
            rerank_results = self.reranker.rerank(
                query=query,
                documents=[
                    document.page_content
                    for document in candidates
                ],
                top_n=self.top_n,
            )

        except RerankError:
            rerank_latency_ms = round(
                (time.perf_counter() - rerank_started) * 1000,
                2,
            )
            logger.exception(
                "重排序失败，降级为原始RRF顺序"
            )

            return [
                copy_document(
                    document,
                    rerank_status="fallback",
                    final_rank=rank,
                    retrieval_latency_ms=retrieval_latency_ms,
                    rerank_latency_ms=rerank_latency_ms,
                    rerank_candidate_count=len(candidates),
                )
                for rank,document in enumerate(
                    candidates[: self.top_n],
                    start=1,
                )
            ]

        rerank_latency_ms = round(
            (time.perf_counter() - rerank_started) * 1000,
            2,
        )
        results: list[Document] = []

        for final_rank, rerank_result in enumerate(
            rerank_results,
            start=1,
        ):
            document = candidates[rerank_result.index]

            results.append(
                copy_document(
                    document,
                    rerank_status="success",
                    rerank_score=(
                        rerank_result.relevance_score
                    ),
                    final_rank=final_rank,
                    retrieval_latency_ms=retrieval_latency_ms,
                    rerank_latency_ms=rerank_latency_ms,
                    rerank_candidate_count=len(candidates),
                )
            )

        return results
