from pathlib import Path

import pytest

from backend.app.rag import split_file


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DOCUMENT_DIRECTORY = (
    PROJECT_ROOT
    / "sample_docs"
    / "fictional_enterprise"
)

DOCUMENTS = sorted(
    DOCUMENT_DIRECTORY.glob("*.docx")
)


@pytest.mark.parametrize(
    "file_path",
    DOCUMENTS,
    ids=lambda path: path.stem,
)
def test_all_docx_files_can_be_split(
    file_path: Path,
) -> None:
    chunks = split_file(file_path)

    assert chunks

    for index, chunk in enumerate(chunks):
        assert chunk.page_content.strip()
        assert chunk.metadata["source"] == str(
            file_path.resolve()
        )
        assert chunk.metadata["document_name"]
        assert chunk.metadata["document_type"]
        assert chunk.metadata["chunk_type"]
        assert chunk.metadata["parent_key"]
        assert chunk.metadata["chunk_in_parent"] >= 0


def test_faq_keeps_question_and_answer_together() -> None:
    file_path = (
        DOCUMENT_DIRECTORY
        / "08_员工与客户服务FAQ.docx"
    )

    chunks = split_file(file_path)

    faq_chunks = [
        chunk
        for chunk in chunks
        if chunk.metadata["chunk_type"] == "faq"
    ]

    assert len(faq_chunks) == 20

    for chunk in faq_chunks:
        assert "Q" in chunk.page_content
        assert "\nA" in chunk.page_content


def test_table_does_not_lose_data_rows() -> None:
    file_path = (
        DOCUMENT_DIRECTORY
        / "02_差旅与费用报销管理制度.docx"
    )

    chunks = split_file(file_path)

    table_text = "\n".join(
        chunk.page_content
        for chunk in chunks
        if chunk.metadata["chunk_type"] == "table"
    )

    assert (
        "A类城市住宿|每晚不超过650元|每晚不超过850元"
        in table_text
    )
    assert (
        "其他城市住宿|每晚不超过380元|每晚不超过500元"
        in table_text
    )