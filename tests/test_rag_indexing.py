from langchain_core.documents import Document

from backend.app import rag


class FakeVectorStore:
    def __init__(self) -> None:
        self.documents: dict[str, Document] = {}

    def get(
        self,
        *,
        where: dict,
        include: list[str],
    ) -> dict:
        source_id = where["source_id"]

        matched = [
            (chunk_id, document)
            for chunk_id, document
            in self.documents.items()
            if document.metadata.get("source_id")
            == source_id
        ]

        return {
            "ids": [
                chunk_id
                for chunk_id, _ in matched
            ],
            "metadatas": [
                document.metadata
                for _, document in matched
            ],
        }

    def add_documents(
        self,
        *,
        documents: list[Document],
        ids: list[str],
    ) -> None:
        for chunk_id, document in zip(ids, documents):
            self.documents[chunk_id] = document

    def delete(self, *, ids: list[str]) -> None:
        for chunk_id in ids:
            self.documents.pop(chunk_id, None)


def test_same_file_is_not_indexed_twice(
    tmp_path,
    monkeypatch,
) -> None:
    file_path = tmp_path / "knowledge.txt"
    file_path.write_text(
        "企业知识库测试内容",
        encoding="utf-8",
    )

    vector_store = FakeVectorStore()

    monkeypatch.setattr(
        rag,
        "Settings",
        lambda: object(),
    )
    monkeypatch.setattr(
        rag,
        "create_embeddings",
        lambda settings: object(),
    )
    monkeypatch.setattr(
        rag,
        "create_vector_store",
        lambda **kwargs: vector_store,
    )
    monkeypatch.setattr(
        rag,
        "split_file",
        lambda path: [
            Document(
                page_content="企业知识库测试内容",
                metadata={
                    "parent_key": "section-1",
                },
            )
        ],
    )

    first_count = rag.index_file(file_path)
    second_count = rag.index_file(file_path)

    assert first_count == 1
    assert second_count == 0
    assert len(vector_store.documents) == 1