import requests
from langchain_core.callbacks import (
    CallbackManagerForRetrieverRun,
)
from langchain_core.documents import Document
from langchain_core.retrievers import BaseRetriever

from backend.app import reranker as reranker_module
from backend.app.rerank_retriever import RerankRetriever
from backend.app.reranker import QwenReranker


class StaticRetriever(BaseRetriever):
    documents: list[Document]

    def _get_relevant_documents(
        self,
        query: str,
        *,
        run_manager: CallbackManagerForRetrieverRun,
    ) -> list[Document]:
        return self.documents


class FakeResponse:
    def __init__(self, data: dict) -> None:
        self._data = data

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._data


def make_candidates() -> list[Document]:
    return [
        Document(
            id=f"chunk-{name}",
            page_content=f"文档内容-{name}",
            metadata={
                "rrf_rank": rank,
                "rrf_score": 1 / (60 + rank),
            },
        )
        for rank, name in enumerate(
            ("a", "b", "c"),
            start=1,
        )
    ]


def make_rerank_retriever() -> RerankRetriever:
    return RerankRetriever(
        base_retriever=StaticRetriever(
            documents=make_candidates()
        ),
        reranker=QwenReranker(
            endpoint="https://example.com/reranks",
            api_key="test-key",
            model="qwen3-rerank",
            timeout_seconds=1,
        ),
        top_n=2,
        search_kwargs={
            "k": 2,
            "fetch_k": 3,
        },
    )


def test_reranker_changes_candidate_order(monkeypatch) -> None:
    def fake_post(
        url: str,
        *,
        headers: dict,
        json: dict,
        timeout: float,
    ) -> FakeResponse:
        assert url == "https://example.com/reranks"
        assert headers["Authorization"] == "Bearer test-key"
        assert json["documents"] == [
            "文档内容-a",
            "文档内容-b",
            "文档内容-c",
        ]
        assert json["top_n"] == 2
        assert timeout == 1

        return FakeResponse({
            "results": [
                {
                    "index": 2,
                    "relevance_score": 0.95,
                },
                {
                    "index": 0,
                    "relevance_score": 0.8,
                },
            ]
        })

    monkeypatch.setattr(
        reranker_module.requests,
        "post",
        fake_post,
    )

    results = make_rerank_retriever().invoke("测试问题")

    assert [
        document.page_content for document in results
    ] == ["文档内容-c", "文档内容-a"]
    assert [
        document.metadata["final_rank"] for document in results
    ] == [1, 2]
    assert [
        document.metadata["rerank_score"] for document in results
    ] == [0.95, 0.8]
    assert all(
        document.metadata["rerank_status"] == "success"
        for document in results
    )
    assert results[0].metadata["rrf_rank"] == 3


def test_reranker_failure_falls_back_to_rrf(monkeypatch) -> None:
    def failing_post(*args, **kwargs):
        raise requests.Timeout("request timed out")

    monkeypatch.setattr(
        reranker_module.requests,
        "post",
        failing_post,
    )

    results = make_rerank_retriever().invoke("测试问题")

    assert [
        document.page_content for document in results
    ] == ["文档内容-a", "文档内容-b"]
    assert [
        document.metadata["final_rank"] for document in results
    ] == [1, 2]
    assert all(
        document.metadata["rerank_status"] == "fallback"
        for document in results
    )
    assert all(
        "rerank_score" not in document.metadata
        for document in results
    )
