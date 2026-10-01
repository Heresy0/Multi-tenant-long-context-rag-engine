import csv
import io
import json
import locale
import re

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping
from urllib.parse import urlparse
from uuid import UUID


ACCESS_TOKEN_ENV = "ENTERPRISE_KB_ACCESS_TOKEN"
LOAD_TEST_MODES = frozenset({"steady", "governance"})
STATUS_NAME_PATTERN = re.compile(
    r"\[(\d{3}(?:-[a-z_]+)?)\]$"
)
SERVER_ERROR_PATTERN = re.compile(r"\b5\d\d\b")


def _required(
    values: Mapping[str, str],
    name: str,
) -> str:
    value = values.get(name, "").strip()
    if not value:
        raise ValueError(f"缺少环境变量：{name}")
    return value


def _positive_float(
    values: Mapping[str, str],
    name: str,
    default: str,
) -> float:
    try:
        value = float(values.get(name, default))
    except ValueError as exc:
        raise ValueError(f"{name} 必须是数字") from exc
    if value <= 0:
        raise ValueError(f"{name} 必须大于 0")
    return value


def load_questions(
    path: Path,
    *,
    categories: frozenset[str] = frozenset(),
) -> tuple[str, ...]:
    questions: list[str] = []

    with path.open("r", encoding="utf-8-sig") as source:
        for line_number, line in enumerate(source, start=1):
            if not line.strip():
                continue
            try:
                case = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"题集第 {line_number} 行不是有效 JSON"
                ) from exc

            if categories and case.get("category") not in categories:
                continue

            question = case.get("question")
            if not isinstance(question, str) or not question.strip():
                raise ValueError(
                    f"题集第 {line_number} 行缺少有效 question"
                )
            questions.append(question.strip())

    if not questions:
        raise ValueError("压测题集为空或没有匹配的分类")
    return tuple(questions)


@dataclass(frozen=True, slots=True)
class LoadTestConfig:
    base_url: str
    access_token: str
    knowledge_base_id: UUID
    questions: tuple[str, ...]
    mode: str
    wait_min_seconds: float
    wait_max_seconds: float
    request_timeout_seconds: float

    @classmethod
    def from_environment(
        cls,
        values: Mapping[str, str],
        *,
        project_root: Path,
    ) -> "LoadTestConfig":
        base_url = values.get(
            "LOAD_TEST_BASE_URL",
            "http://127.0.0.1:8000",
        ).strip().rstrip("/")
        parsed_url = urlparse(base_url)
        if (
            parsed_url.scheme not in {"http", "https"}
            or not parsed_url.netloc
        ):
            raise ValueError(
                "LOAD_TEST_BASE_URL 必须是有效的 HTTP URL"
            )

        token = _required(values, ACCESS_TOKEN_ENV)
        if token.lower().startswith("bearer "):
            token = token[7:].strip()
        if len(token.split(".")) != 3:
            raise ValueError(
                f"{ACCESS_TOKEN_ENV} 不是有效的 JWT"
            )

        try:
            knowledge_base_id = UUID(
                _required(
                    values,
                    "LOAD_TEST_KNOWLEDGE_BASE_ID",
                )
            )
        except ValueError as exc:
            raise ValueError(
                "LOAD_TEST_KNOWLEDGE_BASE_ID 必须是 UUID"
            ) from exc

        mode = values.get(
            "LOAD_TEST_MODE",
            "steady",
        ).strip().lower()
        if mode not in LOAD_TEST_MODES:
            raise ValueError(
                "LOAD_TEST_MODE 必须是 steady 或 governance"
            )

        dataset_value = values.get(
            "LOAD_TEST_QUESTIONS_FILE",
            "evals/datasets/answer_test.jsonl",
        ).strip()
        dataset_path = Path(dataset_value)
        if not dataset_path.is_absolute():
            dataset_path = project_root / dataset_path
        dataset_path = dataset_path.resolve()
        if not dataset_path.is_file():
            raise ValueError(
                f"压测题集不存在：{dataset_path}"
            )

        categories = frozenset(
            item.strip()
            for item in values.get(
                "LOAD_TEST_CATEGORIES",
                "",
            ).split(",")
            if item.strip()
        )
        wait_min = _positive_float(
            values,
            "LOAD_TEST_WAIT_MIN_SECONDS",
            "0.5",
        )
        wait_max = _positive_float(
            values,
            "LOAD_TEST_WAIT_MAX_SECONDS",
            "1.5",
        )
        if wait_max < wait_min:
            raise ValueError(
                "LOAD_TEST_WAIT_MAX_SECONDS 不能小于最小等待时间"
            )

        return cls(
            base_url=base_url,
            access_token=token,
            knowledge_base_id=knowledge_base_id,
            questions=load_questions(
                dataset_path,
                categories=categories,
            ),
            mode=mode,
            wait_min_seconds=wait_min,
            wait_max_seconds=wait_max,
            request_timeout_seconds=_positive_float(
                values,
                "LOAD_TEST_REQUEST_TIMEOUT_SECONDS",
                "120",
            ),
        )


@dataclass(frozen=True, slots=True)
class QaResponseValidation:
    name: str
    successful: bool
    message: str | None = None


def validate_qa_response(
    *,
    status_code: int,
    payload: object,
    request_id: str | None,
    mode: str,
    response_headers: Mapping[str, str] | None = None,
) -> QaResponseValidation:
    status_label = str(status_code)

    if status_code == 429 and response_headers is not None:
        normalized_headers = {
            key.lower(): value
            for key, value in response_headers.items()
        }
        if "x-concurrency-limit" in normalized_headers:
            status_label = "429-concurrency"
        elif "x-ratelimit-limit" in normalized_headers:
            status_label = "429-rate"

    name = f"/api/qa [{status_label}]"

    if status_code == 429:
        return QaResponseValidation(
            name=name,
            successful=mode == "governance",
            message=(
                None
                if mode == "governance"
                else "steady_load_throttled"
            ),
        )

    if status_code != 200:
        return QaResponseValidation(
            name=name,
            successful=False,
            message=f"unexpected_http_status:{status_code}",
        )

    if not isinstance(payload, dict):
        return QaResponseValidation(
            name=name,
            successful=False,
            message="response_not_json_object",
        )
    if type(payload.get("answerable")) is not bool:
        return QaResponseValidation(
            name=name,
            successful=False,
            message="invalid_answerable_field",
        )
    if not isinstance(payload.get("citations"), list):
        return QaResponseValidation(
            name=name,
            successful=False,
            message="invalid_citations_field",
        )
    if not isinstance(payload.get("timings"), dict):
        return QaResponseValidation(
            name=name,
            successful=False,
            message="invalid_timings_field",
        )
    if not request_id:
        return QaResponseValidation(
            name=name,
            successful=False,
            message="missing_request_id",
        )
    return QaResponseValidation(
        name=name,
        successful=True,
    )


def _number(
    row: Mapping[str, str],
    *names: str,
) -> float:
    for name in names:
        value = row.get(name)
        if value not in (None, ""):
            return float(value)
    return 0.0


def _read_csv_rows(path: Path) -> list[dict[str, str]]:
    raw = path.read_bytes()
    encodings = (
        "utf-8-sig",
        locale.getpreferredencoding(False),
        "gb18030",
    )
    attempted: set[str] = set()
    for encoding in encodings:
        normalized = encoding.lower()
        if normalized in attempted:
            continue
        attempted.add(normalized)
        try:
            text = raw.decode(encoding)
        except UnicodeDecodeError:
            continue
        return list(csv.DictReader(io.StringIO(text)))
    raise ValueError(f"无法识别 CSV 编码：{path}")


def read_locust_stats(path: Path) -> dict[str, object]:
    rows = _read_csv_rows(path)
    if not rows:
        raise ValueError("Locust stats CSV 为空")

    aggregate = next(
        (
            row
            for row in rows
            if row.get("Name") in {"Aggregated", "Total"}
        ),
        None,
    )
    if aggregate is None:
        raise ValueError("Locust stats CSV 缺少 Aggregated 行")

    status_counts: dict[str, int] = {}
    outcomes: dict[str, dict[str, float | int]] = {}
    for row in rows:
        match = STATUS_NAME_PATTERN.search(row.get("Name", ""))
        if match:
            status_label = match.group(1)
            count = int(_number(row, "Request Count"))
            status_counts[status_label] = (
                status_counts.get(status_label, 0) + count
            )
            outcomes[status_label] = {
                "request_count": count,
                "failure_count": int(
                    _number(row, "Failure Count")
                ),
                "average_ms": _number(
                    row,
                    "Average Response Time",
                ),
                "median_ms": _number(
                    row,
                    "Median Response Time",
                    "50%",
                ),
                "p95_ms": _number(row, "95%"),
                "p99_ms": _number(row, "99%"),
                "max_ms": _number(
                    row,
                    "Max Response Time",
                    "100%",
                ),
            }

    request_count = int(_number(aggregate, "Request Count"))
    failure_count = int(_number(aggregate, "Failure Count"))
    return {
        "request_count": request_count,
        "failure_count": failure_count,
        "failure_rate": (
            failure_count / request_count
            if request_count
            else 0.0
        ),
        "requests_per_second": _number(
            aggregate,
            "Requests/s",
        ),
        "average_ms": _number(
            aggregate,
            "Average Response Time",
        ),
        "median_ms": _number(
            aggregate,
            "Median Response Time",
            "50%",
        ),
        "p95_ms": _number(aggregate, "95%"),
        "p99_ms": _number(aggregate, "99%"),
        "max_ms": _number(
            aggregate,
            "Max Response Time",
            "100%",
        ),
        "status_counts": status_counts,
        "outcomes": outcomes,
    }


def count_server_errors(path: Path | None) -> int:
    if path is None or not path.is_file():
        return 0
    return sum(
        int(_number(row, "Occurrences", "Count"))
        for row in _read_csv_rows(path)
        if SERVER_ERROR_PATTERN.search(row.get("Error", ""))
    )


@dataclass(frozen=True, slots=True)
class LoadTestThresholds:
    min_requests: int = 1
    min_successful_requests: int = 1
    max_failure_rate: float = 0.01
    max_p95_ms: float = 15_000
    max_server_errors: int = 0
    min_throttled_requests: int = 0


def build_load_test_report(
    *,
    stats_path: Path,
    failures_path: Path | None,
    thresholds: LoadTestThresholds,
    scenario: str,
) -> dict[str, object]:
    summary = read_locust_stats(stats_path)
    server_errors = count_server_errors(failures_path)
    status_counts = summary.get("status_counts")
    if not isinstance(status_counts, dict):
        raise ValueError("Locust 汇总缺少状态码统计")
    throttled = sum(
        int(count)
        for status, count in status_counts.items()
        if str(status).startswith("429")
    )
    request_count = int(summary["request_count"])
    failure_rate = float(summary["failure_rate"])
    outcomes = summary.get("outcomes")
    if not isinstance(outcomes, dict):
        raise ValueError("Locust 汇总缺少按状态分类的指标")
    successful = outcomes.get("200")
    successful_request_count = (
        int(successful["request_count"])
        if isinstance(successful, dict)
        else 0
    )
    successful_p95_ms = (
        float(successful["p95_ms"])
        if isinstance(successful, dict)
        else None
    )
    checks = {
        "minimum_requests": (
            request_count >= thresholds.min_requests
        ),
        "minimum_successful_requests": (
            successful_request_count
            >= thresholds.min_successful_requests
        ),
        "failure_rate": (
            failure_rate <= thresholds.max_failure_rate
        ),
        "p95_latency": (
            successful_p95_ms is not None
            and successful_p95_ms
            <= thresholds.max_p95_ms
        ),
        "server_errors": (
            server_errors <= thresholds.max_server_errors
        ),
        "throttled_requests": (
            throttled >= thresholds.min_throttled_requests
        ),
    }
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "scenario": scenario,
        "passed": all(checks.values()),
        "summary": {
            **summary,
            "server_error_count": server_errors,
            "throttled_request_count": throttled,
            "successful_request_count": (
                successful_request_count
            ),
            "successful_p95_ms": successful_p95_ms,
        },
        "thresholds": {
            "min_requests": thresholds.min_requests,
            "min_successful_requests": (
                thresholds.min_successful_requests
            ),
            "max_failure_rate": thresholds.max_failure_rate,
            "max_p95_ms": thresholds.max_p95_ms,
            "max_server_errors": thresholds.max_server_errors,
            "min_throttled_requests": (
                thresholds.min_throttled_requests
            ),
        },
        "checks": checks,
    }
