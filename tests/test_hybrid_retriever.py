from langchain_core.callbacks import (
    CallbackManagerForRetrieverRun,
)
from langchain_core.documents import Document
from langchain_core.retrievers import BaseRetriever
from pydantic import Field

from backend.app.hybrid_retriever import (
    HybridRetriever,
    document_key,
    tokenize_chinese,
)


class StaticRetriever(BaseRetriever):
    documents: list[Document]
    k: int = 30
    search_kwargs: dict[str, int] = Field(
        default_factory=dict
    )

    def _get_relevant_documents(
        self,
        query: str,
        *,
        run_manager: CallbackManagerForRetrieverRun,
    ) -> list[Document]:
        return self.documents[: self.k]


def make_document(name: str) -> Document:
    return Document(
        page_content=f"文档内容-{name}",
        metadata={
            "source_id": "source-1",
            "chunk_content_hash": name,
        },
    )


def test_tokenizer_keeps_numbers_and_identifiers() -> None:
    tokens = tokenize_chinese(
        "接口返回 RATE-001 和 HTTP 429，"
        "月度可用性为99.2%。"
    )

    assert "rate-001" in tokens
    assert "429" in tokens
    assert "99.2%" in tokens


def test_document_key_uses_source_and_hash() -> None:
    document = make_document("chunk-a")

    assert document_key(document) == (
        "source-1:chunk-a"
    )


def test_rrf_merges_duplicates_and_limits_top_k() -> None:
    document_a = make_document("a")
    document_b = make_document("b")
    document_c = make_document("c")

    retriever = HybridRetriever(
        vector_retriever=StaticRetriever(
            documents=[
                document_a,
                document_b,
            ]
        ),
        keyword_retriever=StaticRetriever(
            documents=[
                document_b,
                document_c,
            ]
        ),
        vector_weight=0.6,
        keyword_weight=0.4,
        search_kwargs={
            "k": 2,
            "fetch_k": 3,
        },
    )

    results = retriever.invoke("测试问题")

    assert len(results) == 2

    result_keys = [
        document_key(document)
        for document in results
    ]

    # document_b 同时被两路召回，RRF融合后应排第一
    assert result_keys[0] == "source-1:b"

    # 重复结果只能出现一次
    assert len(result_keys) == len(set(result_keys))


def test_vector_weight_is_used() -> None:
    document_a = make_document("a")
    document_b = make_document("b")

    retriever = HybridRetriever(
        vector_retriever=StaticRetriever(
            documents=[document_a]
        ),
        keyword_retriever=StaticRetriever(
            documents=[document_b]
        ),
        vector_weight=0.9,
        keyword_weight=0.1,
        search_kwargs={
            "k": 2,
            "fetch_k": 2,
        },
    )

    results = retriever.invoke("测试问题")

    assert document_key(results[0]) == "source-1:a"