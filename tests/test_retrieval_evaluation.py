import json
from pathlib import Path

import pytest

from backend.app.document_splitter import split_docx
from scripts.inspect_retrieval import (
    load_dataset,
    percentile,
    summarize,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_percentile_uses_linear_interpolation() -> None:
    values = [50.0, 10.0, 40.0, 20.0, 30.0]

    assert percentile(values, 0.50) == 30.0
    assert percentile(values, 0.95) == 48.0
    assert percentile([], 0.95) is None

    with pytest.raises(ValueError):
        percentile(values, 1.01)


def test_summarize_includes_latency_percentiles() -> None:
    results = [
        {
            "status": "ok",
            "answerable": True,
            "predicted_answerable": True,
            "hit_at_k": True,
            "reciprocal_rank": 1.0,
            "latency_ms": latency,
            "retrieval_latency_ms": latency / 5,
            "rerank_latency_ms": latency * 4 / 5,
        }
        for latency in (10.0, 20.0, 30.0, 40.0, 50.0)
    ]
    results.append({
        "status": "error",
        "answerable": True,
        "latency_ms": 1000.0,
    })

    summary = summarize(results, k=8)

    assert summary["mean_latency_ms"] == 30.0
    assert summary["p50_latency_ms"] == 30.0
    assert summary["p95_latency_ms"] == 48.0
    assert summary["mean_retrieval_latency_ms"] == 6.0
    assert summary["mean_rerank_latency_ms"] == 24.0


def test_summarize_scores_answerability_predictions() -> None:
    results = [
        {
            "status": "ok",
            "answerable": True,
            "predicted_answerable": True,
            "hit_at_k": True,
            "reciprocal_rank": 1.0,
            "latency_ms": 10.0,
        },
        {
            "status": "ok",
            "answerable": True,
            "predicted_answerable": False,
            "hit_at_k": True,
            "reciprocal_rank": 1.0,
            "latency_ms": 10.0,
        },
        {
            "status": "ok",
            "answerable": False,
            "predicted_answerable": False,
            "hit_at_k": None,
            "reciprocal_rank": None,
            "latency_ms": 10.0,
        },
        {
            "status": "ok",
            "answerable": False,
            "predicted_answerable": True,
            "hit_at_k": None,
            "reciprocal_rank": None,
            "latency_ms": 10.0,
        },
    ]

    summary = summarize(results, k=8)

    assert summary["answerability_accuracy"] == 0.5
    assert summary["answerable_accept_rate"] == 0.5
    assert summary["unanswerable_rejection_rate"] == 0.5
    assert summary["unanswerable_false_accept_rate"] == 0.5
    assert summary["answerability_confusion"] == {
        "true_positive": 1,
        "false_negative": 1,
        "true_negative": 1,
        "false_positive": 1,
    }


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
