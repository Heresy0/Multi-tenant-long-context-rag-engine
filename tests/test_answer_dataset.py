import json
import unicodedata
from collections import Counter
from pathlib import Path

import pytest

from backend.app.document_splitter import split_docx


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATASET_DIRECTORY = PROJECT_ROOT / "evals" / "datasets"
DOCUMENT_DIRECTORY = (
    PROJECT_ROOT
    / "sample_docs"
    / "fictional_enterprise"
)
ANSWERABLE_CATEGORIES = {
    "hr",
    "finance",
    "security",
    "procurement",
    "project",
    "technical",
    "contract",
    "faq",
}
REQUIRED_FIELDS = {
    "id",
    "category",
    "difficulty",
    "question",
    "answerable",
    "reference_answer",
    "required_facts",
    "expected_evidence",
}


def normalize(text: str) -> str:
    return "".join(
        unicodedata.normalize("NFKC", text).split()
    )


def load_jsonl(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(
            encoding="utf-8-sig"
        ).splitlines()
        if line.strip()
    ]


@pytest.fixture(scope="module")
def chunks_by_document() -> dict[str, list[str]]:
    return {
        path.name: [
            document.page_content
            for document in split_docx(path)
        ]
        for path in DOCUMENT_DIRECTORY.glob("*.docx")
    }


@pytest.mark.parametrize(
    "dataset_name",
    ["answer_dev.jsonl", "answer_test.jsonl"],
)
def test_answer_dataset_structure_and_counts(
    dataset_name: str,
) -> None:
    cases = load_jsonl(
        DATASET_DIRECTORY / dataset_name
    )

    assert len(cases) == 32
    assert len({case["id"] for case in cases}) == 32
    assert sum(
        case["answerable"] is True
        for case in cases
    ) == 24
    assert sum(
        case["answerable"] is False
        for case in cases
    ) == 8

    for case in cases:
        assert set(case) == REQUIRED_FIELDS
        assert case["id"].strip()
        assert case["question"].strip()
        assert case["reference_answer"].strip()
        assert type(case["answerable"]) is bool
        assert case["difficulty"] in {
            "easy",
            "medium",
            "hard",
        }
        assert isinstance(case["required_facts"], list)
        assert isinstance(case["expected_evidence"], list)

        if case["answerable"]:
            assert case["category"] in ANSWERABLE_CATEGORIES
            assert case["required_facts"]
            assert case["expected_evidence"]
        else:
            assert case["category"] == "unanswerable"
            assert case["required_facts"] == []
            assert case["expected_evidence"] == []


@pytest.mark.parametrize(
    "dataset_name",
    ["answer_dev.jsonl", "answer_test.jsonl"],
)
def test_required_facts_are_present_in_reference_answer(
    dataset_name: str,
) -> None:
    cases = load_jsonl(
        DATASET_DIRECTORY / dataset_name
    )

    for case in cases:
        normalized_answer = normalize(
            case["reference_answer"]
        )

        for fact in case["required_facts"]:
            assert normalize(fact) in normalized_answer, (
                case["id"],
                fact,
            )


@pytest.mark.parametrize(
    "dataset_name",
    ["answer_dev.jsonl", "answer_test.jsonl"],
)
def test_expected_evidence_exists_in_source_documents(
    dataset_name: str,
    chunks_by_document: dict[str, list[str]],
) -> None:
    cases = load_jsonl(
        DATASET_DIRECTORY / dataset_name
    )

    for case in cases:
        for evidence in case["expected_evidence"]:
            document_name = evidence.get("document")
            evidence_text = evidence.get("text")

            assert document_name in chunks_by_document, (
                case["id"],
                document_name,
            )
            assert isinstance(evidence_text, str)
            assert evidence_text.strip()
            assert any(
                normalize(evidence_text)
                in normalize(content)
                for content in chunks_by_document[
                    document_name
                ]
            ), (case["id"], evidence_text)


def test_dev_and_test_sets_are_disjoint_and_balanced() -> None:
    dev_cases = load_jsonl(
        DATASET_DIRECTORY / "answer_dev.jsonl"
    )
    test_cases = load_jsonl(
        DATASET_DIRECTORY / "answer_test.jsonl"
    )

    assert {
        case["id"] for case in dev_cases
    }.isdisjoint({
        case["id"] for case in test_cases
    })
    assert {
        case["question"] for case in dev_cases
    }.isdisjoint({
        case["question"] for case in test_cases
    })

    for cases in (dev_cases, test_cases):
        category_counts = Counter(
            case["category"]
            for case in cases
            if case["answerable"]
        )

        assert category_counts == {
            category: 3
            for category in ANSWERABLE_CATEGORIES
        }
