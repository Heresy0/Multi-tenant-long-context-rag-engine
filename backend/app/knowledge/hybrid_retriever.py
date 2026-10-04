from __future__ import annotations

from langchain_community.retrievers import BM25Retriever
from langchain_core.callbacks import CallbackManagerForRetrieverRun
from langchain_core.documents import Document
from langchain_core.retrievers import BaseRetriever
from pydantic import Field

from .keyword_tokenizer import tokenize_chinese
from ..evaluation.trace import record_documents


def document_key(document: Document) -> str:
    """确保向量结果和BM25结果中的同一分块能够合并。"""
    source_id = document.metadata.get("source_id")
    content_hash = document.metadata.get(
        "chunk_content_hash"
    )

    if source_id and content_hash:
        return f"{source_id}:{content_hash}"

    return document.page_content


class HybridRetriever(BaseRetriever):
    """使用加权RRF融合向量检索和BM25检索。"""

    vector_retriever: BaseRetriever
    keyword_retriever: BaseRetriever

    vector_weight: float = 0.6
    keyword_weight: float = 0.4
    rrf_c: int = 60

    #保持与现有评估脚本兼容
    search_type: str = "hybrid_rrf"
    search_kwargs: dict[str, int] = Field(
        default_factory=lambda: {
            "k": 8,
            "fetch_k": 30,
        }
    )

    def _get_relevant_documents(
            self,
            query:str,
            *,
            run_manager: CallbackManagerForRetrieverRun,
    ) -> list[Document]:
        k = int(self.search_kwargs.get("k",8))

        vector_documents = self.vector_retriever.invoke(
            query
        )
        record_documents("vector", vector_documents)
        keyword_documents = self.keyword_retriever.invoke(
            query
        )
        record_documents("keyword", keyword_documents)

        scores: dict[str, float] = {}
        documents: dict[str,Document] = {}

        retrieval_results = (
            (
                vector_documents,
                self.vector_weight,
            ),
            (
                keyword_documents,
                self.keyword_weight,
            ),
        )

        for result_list,weight in retrieval_results:
            for rank,document in enumerate(
                result_list,
                start=1,
            ):
                key = document_key(document)

                scores[key] = (
                    scores.get(key, 0.0)
                    +weight/(self.rrf_c + rank)
                )

                documents.setdefault(key,document)

        sorted_keys = sorted(
            scores,
            key=scores.get,
            reverse=True,
        )

        results: list[Document] = []

        for rrf_rank, key in enumerate(
            sorted_keys[:k],
            start=1,
        ):
            document = documents[key]
            metadata = dict(document.metadata)

            metadata.update({
                "rrf_score": scores[key],
                "rrf_rank": rrf_rank,
            })

            kwargs = {
                "page_content": document.page_content,
                "metadata": metadata,
            }

            document_id = getattr(document, "id", None)

            if document_id is not None:
                kwargs["id"] = document_id

            results.append(Document(**kwargs))

        record_documents("fusion", results)
        return results

def create_hybrid_retriever(
        *,
        vector_retriever: BaseRetriever,
        keyword_retriever: BaseRetriever | None = None,
        keyword_documents: list[Document] | None = None,
        k: int = 8,
        fetch_k: int = 30,
) -> BaseRetriever:
    """正式链路注入数据库关键词检索器；离线评估仍可传入小型语料。"""
    if keyword_retriever is not None and keyword_documents is not None:
        raise ValueError("不能同时提供关键词检索器和关键词语料。")
    if keyword_retriever is None:
        if not keyword_documents:
            raise RuntimeError("知识库为空，无法创建BM25检索器。")
        keyword_retriever = BM25Retriever.from_documents(
            keyword_documents, preprocess_func=tokenize_chinese, k=fetch_k,
        )

    return HybridRetriever(
        vector_retriever=vector_retriever,
        keyword_retriever=keyword_retriever,
        vector_weight=0.6,
        keyword_weight=0.4,
        search_kwargs={
            "k":k,
            "fetch_k":fetch_k,
        },
    )

