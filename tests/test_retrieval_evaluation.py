import json
from pathlib import Path

from backend.app.documents.document_splitter import split_docx


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def load_dataset(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(
            encoding="utf-8-sig"
        ).splitlines()
        if line.strip()
    ]


def test_independent_dataset_has_valid_evidence() -> None:
    dataset_path = (
        PROJECT_ROOT
        / "evals"
        / "datasets"
        / "retrieval_test.jsonl"
    )
    document_root = (
        PROJECT_ROOT
        / "sample_docs"
        / "fictional_enterprise"
    )
    cases = load_dataset(dataset_path)
    chunks_by_document = {
        path.name: [
            document.page_content
            for document in split_docx(path)
        ]
        for path in document_root.glob("*.docx")
    }

    assert len(cases) == 32
    assert sum(case["answerable"] for case in cases) == 24

    for case in cases:
        for evidence in case["expected_evidence"]:
            assert any(
                evidence["text"] in content
                for content in chunks_by_document[
                    evidence["document"]
                ]
            ), case["id"]

    dev_questions = {
        json.loads(line)["question"]
        for line in (
            PROJECT_ROOT
            / "evals"
            / "datasets"
            / "retrieval_dev.jsonl"
        ).read_text(encoding="utf-8").splitlines()
        if line.strip()
    }
    test_questions = {
        case["question"]
        for case in cases
    }

    assert dev_questions.isdisjoint(test_questions)
