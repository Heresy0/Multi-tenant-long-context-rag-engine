from pathlib import Path

import pytest
from langchain_core.documents import Document

from backend.app import retrieval_service as service_module
from backend.app.retrieval_service import RetrievalService


class FakeRetriever:
    def __init__(self, name: str) -> None:
        self.name = name
        self.queries: list[str] = []

    def invoke(self, query: str) -> list[Document]:
        self.queries.append(query)
        return [
            Document(
                page_content=self.name,
                metadata={"retriever": self.name},
            )
        ]


def test_search_lazily_builds_and_reuses_retriever(
    monkeypatch,
) -> None:
    built: list[FakeRetriever] = []

    def fake_create_retriever(**kwargs) -> FakeRetriever:
        retriever = FakeRetriever(f"retriever-{len(built) + 1}")
        built.append(retriever)
        return retriever

    monkeypatch.setattr(
        service_module,
        "create_retriever",
        fake_create_retriever,
    )

    service = RetrievalService(settings=object())

    assert built == []
    assert service.search("question-1")[0].page_content == "retriever-1"
    assert service.search("question-2")[0].page_content == "retriever-1"
    assert len(built) == 1
    assert built[0].queries == ["question-1", "question-2"]


def test_refresh_replaces_retriever(monkeypatch) -> None:
    built: list[FakeRetriever] = []

    def fake_create_retriever(**kwargs) -> FakeRetriever:
        retriever = FakeRetriever(f"retriever-{len(built) + 1}")
        built.append(retriever)
        return retriever

    monkeypatch.setattr(
        service_module,
        "create_retriever",
        fake_create_retriever,
    )

    service = RetrievalService(settings=object())

    assert service.search("before")[0].page_content == "retriever-1"

    service.refresh()

    assert service.search("after")[0].page_content == "retriever-2"
    assert len(built) == 2


def test_failed_refresh_keeps_previous_retriever(
    monkeypatch,
) -> None:
    original = FakeRetriever("original")
    attempts = 0

    def fake_create_retriever(**kwargs) -> FakeRetriever:
        nonlocal attempts
        attempts += 1

        if attempts == 1:
            return original

        raise RuntimeError("rebuild failed")

    monkeypatch.setattr(
        service_module,
        "create_retriever",
        fake_create_retriever,
    )

    service = RetrievalService(settings=object())
    service.search("before")

    with pytest.raises(RuntimeError, match="rebuild failed"):
        service.refresh()

    assert service.search("after")[0].page_content == "original"
    assert original.queries == ["before", "after"]


@pytest.mark.parametrize(
    ("written_count", "expected_builds"),
    [
        (0, 0),
        (3, 1),
    ],
)
def test_index_file_refreshes_only_when_data_changed(
    monkeypatch,
    tmp_path: Path,
    written_count: int,
    expected_builds: int,
) -> None:
    build_count = 0
    indexed: list[tuple[Path, object]] = []
    settings = object()

    def fake_create_retriever(**kwargs) -> FakeRetriever:
        nonlocal build_count
        build_count += 1
        return FakeRetriever("refreshed")

    def fake_index_file(
        file_path: str | Path,
        *,
        settings: object,
    ) -> int:
        indexed.append((Path(file_path), settings))
        return written_count

    monkeypatch.setattr(
        service_module,
        "create_retriever",
        fake_create_retriever,
    )
    monkeypatch.setattr(
        service_module,
        "index_rag_file",
        fake_index_file,
    )

    service = RetrievalService(settings=settings)
    file_path = tmp_path / "knowledge.docx"

    result = service.index_file(file_path)

    assert result == written_count
    assert indexed == [(file_path, settings)]
    assert build_count == expected_builds
