import json
from pathlib import Path
from uuid import UUID

import pytest
import requests

from scripts.evaluate_answers import (
    ACCESS_TOKEN_ENV,
    evaluate_case,
    filter_cases,
    load_access_token,
    load_dataset,
    normalize,
    percentile,
    source_name,
    summarize,
)


KNOWLEDGE_BASE_ID = UUID(
    "00000000-0000-0000-0000-000000000001"
)
ACCESS_TOKEN = "header.payload.signature"


class FakeResponse:
    def __init__(
        self,
        data: dict,
    ) -> None:
        self._data = data

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._data


def make_case() -> dict:
    return {
        "id": "answer-001",
        "category": "contract",
        "difficulty": "hard",
        "question": "可用性为99.2%时抵扣多少？",
        "answerable": True,
        "reference_answer": "可以抵扣当月服务费的10%。",
        "required_facts": ["10%"],
        "expected_evidence": [
            {
                "document": "07_合同.docx",
                "text": "低于99.5%但不低于99.0%|10%",
            }
        ],
    }


def test_normalization_and_source_name() -> None:
    assert normalize(" ９９．２％ \n") == "99.2%"
    assert source_name(r"C:\docs\policy.docx") == (
        "policy.docx"
    )
    assert source_name("/data/policy.docx") == (
        "policy.docx"
    )
    assert source_name(
        "file:///D:/docs/06_%E5%BC%80%E6%94%BE"
        "%E5%B9%B3%E5%8F%B0.docx"
    ) == "06_开放平台.docx"


def test_access_token_is_loaded_only_from_environment() -> None:
    assert load_access_token({
        ACCESS_TOKEN_ENV: f" Bearer {ACCESS_TOKEN} ",
    }) == ACCESS_TOKEN

    with pytest.raises(
        ValueError,
        match=ACCESS_TOKEN_ENV,
    ):
        load_access_token({})


def test_load_dataset_validates_answer_fields(
    tmp_path: Path,
) -> None:
    dataset_path = tmp_path / "answers.jsonl"
    dataset_path.write_text(
        json.dumps(
            make_case(),
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    cases = load_dataset(dataset_path)

    assert cases == [make_case()]


def test_filters_cases_for_one_knowledge_base() -> None:
    cases = [
        {"id": "technical-1", "category": "technical"},
        {"id": "hr-1", "category": "hr"},
        {"id": "unanswerable-1", "category": "unanswerable"},
    ]

    selected = filter_cases(
        cases,
        ["technical", "unanswerable"],
    )

    assert [case["id"] for case in selected] == [
        "technical-1",
        "unanswerable-1",
    ]


def test_evaluate_case_scores_facts_and_citation_source() -> None:
    calls: list[dict] = []

    def fake_post(
        endpoint: str,
        *,
        headers: dict,
        json: dict,
        timeout: float,
    ) -> FakeResponse:
        calls.append({
            "endpoint": endpoint,
            "headers": headers,
            "json": json,
            "timeout": timeout,
        })
        return FakeResponse({
            "answer": "服务费抵扣比例为10%。[资料1]",
            "answerable": True,
            "citations": [
                {
                    "citation_id": "资料1",
                    "source": r"D:\docs\07_合同.docx",
                }
            ],
            "refusal_reason": None,
            "timings": {
                "hybrid_retrieval_ms": 120.5,
                "rerank_ms": 800.25,
                "search_total_ms": 925.0,
                "context_build_ms": 0.5,
                "generation_ms": 9000.0,
                "validation_ms": 0.3,
                "render_ms": 0.2,
                "total_ms": 9926.0,
            },
        })

    result = evaluate_case(
        "http://localhost/api/qa",
        make_case(),
        knowledge_base_id=KNOWLEDGE_BASE_ID,
        access_token=ACCESS_TOKEN,
        timeout_seconds=30,
        post=fake_post,
    )

    assert calls == [{
        "endpoint": "http://localhost/api/qa",
        "headers": {
            "Authorization": f"Bearer {ACCESS_TOKEN}",
        },
        "json": {
            "knowledge_base_id": str(KNOWLEDGE_BASE_ID),
            "question": "可用性为99.2%时抵扣多少？"
        },
        "timeout": 30,
    }]
    assert result["status"] == "ok"
    assert result["predicted_answerable"] is True
    assert result["matched_facts"] == ["10%"]
    assert result["missing_facts"] == []
    assert result["fact_complete"] is True
    assert result["expected_documents"] == [
        "07_合同.docx"
    ]
    assert result["cited_documents"] == [
        "07_合同.docx"
    ]
    assert result["citation_source_hit"] is True
    assert result["timings"]["rerank_ms"] == 800.25
    assert result["timings"]["generation_ms"] == 9000.0
    assert ACCESS_TOKEN not in json.dumps(result)


def test_evaluate_case_records_request_error() -> None:
    def failing_post(*args, **kwargs):
        raise requests.Timeout("request timed out")

    result = evaluate_case(
        "http://localhost/api/qa",
        make_case(),
        knowledge_base_id=KNOWLEDGE_BASE_ID,
        access_token=ACCESS_TOKEN,
        timeout_seconds=1,
        post=failing_post,
    )

    assert result["status"] == "error"
    assert result["error_type"] == "Timeout"
    assert "request timed out" in result["error"]


def test_percentile_uses_linear_interpolation() -> None:
    values = [10.0, 20.0, 30.0, 40.0]

    assert percentile(values, 0.50) == 25.0
    assert percentile(values, 0.95) == 38.5
    assert percentile([], 0.95) is None


def test_summarize_combines_answer_fact_citation_metrics() -> None:
    results = [
        {
            "status": "ok",
            "expected_answerable": True,
            "predicted_answerable": True,
            "required_facts": ["A", "B"],
            "matched_facts": ["A", "B"],
            "expected_documents": ["a.docx"],
            "cited_documents": ["a.docx"],
            "matched_documents": ["a.docx"],
            "citation_source_hit": True,
            "fact_complete": True,
            "latency_ms": 10.0,
            "timings": {
                "generation_ms": 8.0,
                "total_ms": 9.0,
            },
        },
        {
            "status": "ok",
            "expected_answerable": True,
            "predicted_answerable": False,
            "required_facts": ["C", "D"],
            "matched_facts": [],
            "expected_documents": ["b.docx"],
            "cited_documents": [],
            "matched_documents": [],
            "citation_source_hit": False,
            "fact_complete": False,
            "latency_ms": 20.0,
            "timings": {
                "generation_ms": 16.0,
                "total_ms": 18.0,
            },
        },
        {
            "status": "ok",
            "expected_answerable": False,
            "predicted_answerable": False,
            "required_facts": [],
            "matched_facts": [],
            "expected_documents": [],
            "cited_documents": [],
            "matched_documents": [],
            "citation_source_hit": None,
            "fact_complete": None,
            "latency_ms": 30.0,
            "timings": {},
        },
        {
            "status": "ok",
            "expected_answerable": False,
            "predicted_answerable": True,
            "required_facts": [],
            "matched_facts": [],
            "expected_documents": [],
            "cited_documents": ["wrong.docx"],
            "matched_documents": [],
            "citation_source_hit": None,
            "fact_complete": None,
            "latency_ms": 40.0,
            "timings": {
                "generation_ms": 32.0,
                "total_ms": 36.0,
            },
        },
        {
            "status": "error",
            "expected_answerable": True,
            "latency_ms": 1000.0,
        },
    ]

    summary = summarize(results)

    assert summary["total"] == 5
    assert summary["successful"] == 4
    assert summary["errors"] == 1
    assert summary["answerability_accuracy"] == 0.5
    assert summary["answerable_accept_rate"] == 0.5
    assert summary["unanswerable_rejection_rate"] == 0.5
    assert summary["unanswerable_false_accept_rate"] == 0.5
    assert summary["required_fact_coverage"] == 0.5
    assert summary["fact_complete_rate"] == 0.5
    assert summary["citation_source_precision"] == 1.0
    assert summary["citation_source_recall"] == 0.5
    assert summary["citation_case_hit_rate"] == 0.5
    assert summary["unanswerable_citation_free_rate"] == 0.5
    assert summary["mean_latency_ms"] == 25.0
    assert summary["p50_latency_ms"] == 25.0
    assert summary["p95_latency_ms"] == 38.5
    assert summary["stage_latency"]["generation_ms"] == {
        "count": 3,
        "mean_ms": 18.67,
        "p50_ms": 16.0,
        "p95_ms": 30.4,
    }
    assert summary["stage_latency"]["total_ms"] == {
        "count": 3,
        "mean_ms": 21.0,
        "p50_ms": 18.0,
        "p95_ms": 34.2,
    }
    assert summary["stage_latency"]["rerank_ms"] == {
        "count": 0,
        "mean_ms": None,
        "p50_ms": None,
        "p95_ms": None,
    }
