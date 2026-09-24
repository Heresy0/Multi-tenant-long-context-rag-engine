from __future__ import annotations

import re
import unicodedata

import jieba
from langchain_chroma import Chroma
from langchain_community.retrievers import BM25Retriever
from langchain_core.callbacks import CallbackManagerForRetrieverRun
from langchain_core.documents import Document
from langchain_core.retrievers import BaseRetriever
from pydantic import Field



SPECIAL_TOKEN_PATTERN = re.compile(
    r"[a-z][a-z0-9]*(?:[-_][a-z0-9]+)+"
    r"|\d+(?:\.\d+)?%?",
    flags=re.IGNORECASE,
)


def tokenize_chinese(text: str) -> list[str]:
    """适用于中文、数字、错误码和API标识的BM25分词。"""
    normalized = unicodedata.normalize(
        "NFKC",
        text,
    ).lower()

    tokens = [
        token.strip()
        for token in jieba.cut_for_search(normalized)
        if token.strip()
        and re.search(
            r"[a-z0-9\u4e00-\u9fff]",
            token,
        )
    ]

    # 加强百分比、HTTP状态码和Idempotency-Key等精确词
    tokens.extend(
        SPECIAL_TOKEN_PATTERN.findall(normalized)
    )
    return tokens


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
        fetch_k = int(
            self.search_kwargs.get("fetch_k",30)
        )

        #向量检索先找回较多候选
        vector_search_kwargs = getattr(
            self.vector_retriever,
            "search_kwargs",
            None,
        )

        if vector_search_kwargs is not None:
            vector_search_kwargs.update({
                "k": fetch_k,
                "fetch_k": max(fetch_k * 2, 60),
            })

        self.keyword_retriever.k = fetch_k

        vector_documents = self.vector_retriever.invoke(
            query
        )
        keyword_documents = self.keyword_retriever.invoke(
            query
        )

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

        return [
            documents[key]
            for key in sorted_keys[:k]
        ]

def create_hybrid_retriever(
        vector_store: Chroma,
        *,
        k: int = 8,
        fetch_k: int = 30,
) -> BaseRetriever:
    """从Chroma中的现有分块建立混合检索器。"""
    stored = vector_store.get(
        include = [
            "documents",
            "metadatas",
        ]
    )

    ids = stored.get("ids") or []
    texts = stored.get("documents") or []
    metadatas = stored.get("metadatas") or []

    documents: list[Document] = []

    for chunk_id,text,metadata in zip(
        ids,
        texts,
        metadatas,
    ):
        if not text:
            continue

        chunk_metadata = dict(metadata or {})
        chunk_metadata["chunk_id"] = chunk_id

        documents.append(
            Document(
                id=chunk_id,
                page_content=text,
                metadata=chunk_metadata,
            )
        )

    if not documents:
        raise RuntimeError(
            "知识库为空，无法创建BM25检索器。"
        )

    vector_retriever = vector_store.as_retriever(
        search_type="mmr",
        search_kwargs={
            "k": fetch_k,
            "fetch_k": max(fetch_k * 2, 60),
        },
    )

    keyword_retriever = BM25Retriever.from_documents(
        documents,
        preprocess_func=tokenize_chinese,
        k=fetch_k,
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

