import json

from pathlib import Path
from uuid import uuid4

import pytest

from scripts.load_test_support import (
    LoadTestConfig,
    LoadTestThresholds,
    build_load_test_report,
    validate_qa_response,
)


def _dataset(path: Path) -> Path:
    path.write_text(
        "\n".join([
            json.dumps({
                "category": "technical",
                "question": "访问令牌有效期是多少？",
            }, ensure_ascii=False),
            json.dumps({
                "category": "hr",
                "question": "年假如何申请？",
            }, ensure_ascii=False),
        ]),
        encoding="utf-8",
    )
    return path


def test_load_test_config_reads_secure_environment(
    tmp_path: Path,
) -> None:
    dataset = _dataset(tmp_path / "questions.jsonl")

    config = LoadTestConfig.from_environment(
        {
            "ENTERPRISE_KB_ACCESS_TOKEN": (
                "Bearer header.payload.signature"
            ),
            "LOAD_TEST_KNOWLEDGE_BASE_ID": str(uuid4()),
            "LOAD_TEST_QUESTIONS_FILE": str(dataset),
            "LOAD_TEST_CATEGORIES": "technical",
            "LOAD_TEST_MODE": "governance",
            "LOAD_TEST_WAIT_MIN_SECONDS": "0.2",
            "LOAD_TEST_WAIT_MAX_SECONDS": "0.8",
        },
        project_root=tmp_path,
    )

    assert config.access_token == "header.payload.signature"
    assert config.mode == "governance"
    assert config.questions == (
        "访问令牌有效期是多少？",
    )
    assert config.wait_min_seconds == 0.2
    assert config.wait_max_seconds == 0.8


def test_load_test_config_rejects_invalid_inputs(
    tmp_path: Path,
) -> None:
    dataset = _dataset(tmp_path / "questions.jsonl")
    base = {
        "ENTERPRISE_KB_ACCESS_TOKEN": "a.b.c",
        "LOAD_TEST_KNOWLEDGE_BASE_ID": str(uuid4()),
        "LOAD_TEST_QUESTIONS_FILE": str(dataset),
    }

    with pytest.raises(ValueError, match="steady"):
        LoadTestConfig.from_environment(
            {**base, "LOAD_TEST_MODE": "unknown"},
            project_root=tmp_path,
        )

    with pytest.raises(
        ValueError,
        match="LOAD_TEST_WAIT_MAX_SECONDS",
    ):
        LoadTestConfig.from_environment(
            {
                **base,
                "LOAD_TEST_WAIT_MIN_SECONDS": "2",
                "LOAD_TEST_WAIT_MAX_SECONDS": "1",
            },
            project_root=tmp_path,
        )


def test_qa_response_validation_distinguishes_modes() -> None:
    payload = {
        "answerable": True,
        "citations": [],
        "timings": {},
    }
    successful = validate_qa_response(
        status_code=200,
        payload=payload,
        request_id="request-1",
        mode="steady",
    )
    steady_throttled = validate_qa_response(
        status_code=429,
        payload={"detail": "too many requests"},
        request_id=None,
        mode="steady",
    )
    governance_throttled = validate_qa_response(
        status_code=429,
        payload={"detail": "too many requests"},
        request_id=None,
        mode="governance",
        response_headers={
            "X-Concurrency-Limit": "3",
        },
    )

    assert successful.successful is True
    assert successful.name == "/api/qa [200]"
    assert steady_throttled.successful is False
    assert governance_throttled.successful is True
    assert governance_throttled.name == (
        "/api/qa [429-concurrency]"
    )


def test_report_summarizes_and_enforces_thresholds(
    tmp_path: Path,
) -> None:
    stats = tmp_path / "result_stats.csv"
    stats.write_text(
        "Type,Name,Request Count,Failure Count,Median Response Time,"
        "Average Response Time,Min Response Time,Max Response Time,"
        "Average Content Size,Requests/s,Failures/s,95%,99%,100%\n"
        "POST,/api/qa [200],18,0,7000,7200,6000,10000,500,2.0,0,"
        "9000,9800,10000\n"
        "POST,/api/qa [429-concurrency],2,0,10,10,8,12,100,0.2,0,"
        "12,12,12\n"
        "POST,Aggregated,20,0,6800,6481,8,10000,460,2.2,0,"
        "9000,9800,10000\n",
        encoding="utf-8",
    )
    failures = tmp_path / "result_failures.csv"
    failures.write_text(
        "Method,Name,Error,Occurrences\n",
        encoding="utf-8",
    )

    report = build_load_test_report(
        stats_path=stats,
        failures_path=failures,
        thresholds=LoadTestThresholds(
            min_requests=20,
            min_successful_requests=10,
            max_failure_rate=0,
            max_p95_ms=10_000,
            max_server_errors=0,
            min_throttled_requests=1,
        ),
        scenario="governance",
    )

    assert report["passed"] is True
    summary = report["summary"]
    assert summary["request_count"] == 20
    assert summary["successful_request_count"] == 18
    assert summary["successful_p95_ms"] == 9000
    assert summary["throttled_request_count"] == 2
    assert summary["p95_ms"] == 9000


def test_report_fails_on_latency_failures_and_5xx(
    tmp_path: Path,
) -> None:
    stats = tmp_path / "result_stats.csv"
    stats.write_text(
        "Type,Name,Request Count,Failure Count,Median Response Time,"
        "Average Response Time,Max Response Time,Requests/s,95%,99%\n"
        "POST,Aggregated,10,2,12000,13000,20000,0.5,18000,20000\n",
        encoding="utf-8",
    )
    failures = tmp_path / "result_failures.csv"
    failures.write_bytes(
        (
            "Method,Name,Error,Occurrences\n"
            "POST,/api/qa [500],HTTP 500 服务异常,2\n"
        ).encode("gb18030"),
    )

    report = build_load_test_report(
        stats_path=stats,
        failures_path=failures,
        thresholds=LoadTestThresholds(),
        scenario="steady",
    )

    assert report["passed"] is False
    assert report["checks"] == {
        "minimum_requests": True,
        "minimum_successful_requests": False,
        "failure_rate": False,
        "p95_latency": False,
        "server_errors": False,
        "throttled_requests": True,
    }
    assert report["summary"]["server_error_count"] == 2
