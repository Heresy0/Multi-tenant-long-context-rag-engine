import logging
import time
from collections.abc import Callable

from langchain_core.callbacks import (
    CallbackManagerForRetrieverRun,
)
from langchain_core.documents import Document
from langchain_core.retrievers import BaseRetriever
from pydantic import Field

from .reranker import BaseReranker, RerankError
from .evidence_selection import render_for_rerank


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
    candidate_processor: Callable | None = Field(default=None, exclude=True)
    result_processor: Callable | None = Field(default=None, exclude=True)

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
        if self.candidate_processor is not None:
            candidates = self.candidate_processor(query, candidates)
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
                    render_for_rerank(document) if self.candidate_processor else document.page_content
                    for document in candidates
                ],
                top_n=max(self.top_n, len(candidates)) if self.result_processor else self.top_n,
            )

        except RerankError:
            rerank_latency_ms = round(
                (time.perf_counter() - rerank_started) * 1000,
                2,
            )
            logger.exception(
                "重排序失败，降级为候选顺序（保留适用性选择）"
            )

            fallback = self.result_processor(query, candidates) if self.result_processor else candidates
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
                    fallback[: self.top_n],
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

        if self.result_processor:
            results = self.result_processor(query, results)[:self.top_n]
            for rank, document in enumerate(results, 1):
                document.metadata["final_rank"] = rank
        return results
