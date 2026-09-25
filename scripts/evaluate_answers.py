import argparse
import json
import math
import sys
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

import requests


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))


TIMING_FIELDS = (
    "hybrid_retrieval_ms",
    "rerank_ms",
    "search_total_ms",
    "context_build_ms",
    "generation_ms",
    "validation_ms",
    "render_ms",
    "total_ms",
)


def normalize(text: str) -> str:
    """统一字符形式并忽略空白，用于匹配关键事实。"""
    return "".join(
        unicodedata.normalize("NFKC", text).split()
    )


def source_name(source: str) -> str:
    """同时兼容 Windows 和 Linux 路径。"""
    return source.replace("\\", "/").rsplit("/", 1)[-1]


def load_dataset(path: Path) -> list[dict]:
    cases: list[dict] = []
    seen_ids: set[str] = set()

    with path.open("r", encoding="utf-8-sig") as file:
        for line_number, line in enumerate(file, start=1):
            if not line.strip():
                continue

            case = json.loads(line)

            if not isinstance(case, dict):
                raise ValueError(
                    f"第 {line_number} 行必须是 JSON 对象"
                )

            for field in (
                "id",
                "question",
                "reference_answer",
            ):
                value = case.get(field)
                if not isinstance(value, str) or not value.strip():
                    raise ValueError(
                        f"第 {line_number} 行缺少有效的 {field}"
                    )

            if case["id"] in seen_ids:
                raise ValueError(
                    f"样本 ID 重复：{case['id']}"
                )

            if type(case.get("answerable")) is not bool:
                raise ValueError(
                    f"{case['id']} 的 answerable 必须是布尔值"
                )

            required_facts = case.get("required_facts")
            expected_evidence = case.get("expected_evidence")

            if not isinstance(required_facts, list) or not all(
                isinstance(fact, str) and fact.strip()
                for fact in required_facts
            ):
                raise ValueError(
                    f"{case['id']} 的 required_facts 格式无效"
                )

            if not isinstance(expected_evidence, list):
                raise ValueError(
                    f"{case['id']} 的 expected_evidence 必须是列表"
                )

            for evidence in expected_evidence:
                if not isinstance(evidence, dict) or not all(
                    isinstance(evidence.get(key), str)
                    and evidence[key].strip()
                    for key in ("document", "text")
                ):
                    raise ValueError(
                        f"{case['id']} 的证据格式无效"
                    )

            if case["answerable"]:
                if not required_facts:
                    raise ValueError(
                        f"{case['id']} 可回答但没有关键事实"
                    )
                if not expected_evidence:
                    raise ValueError(
                        f"{case['id']} 可回答但没有正确证据"
                    )
            elif required_facts or expected_evidence:
                raise ValueError(
                    f"{case['id']} 不可回答，不应标注事实或证据"
                )

            seen_ids.add(case["id"])
            cases.append(case)

    if not cases:
        raise ValueError("题集为空")

    return cases


def validate_response(data) -> dict:
    if not isinstance(data, dict):
        raise ValueError("接口响应必须是 JSON 对象")

    if not isinstance(data.get("answer"), str):
        raise ValueError("接口响应缺少 answer 字符串")

    if type(data.get("answerable")) is not bool:
        raise ValueError("接口响应缺少 answerable 布尔值")

    citations = data.get("citations")
    if not isinstance(citations, list):
        raise ValueError("接口响应缺少 citations 列表")

    for citation in citations:
        if not isinstance(citation, dict):
            raise ValueError("citation 必须是 JSON 对象")
        if not isinstance(citation.get("source"), str):
            raise ValueError("citation 缺少 source 字符串")

    refusal_reason = data.get("refusal_reason")
    if refusal_reason is not None and not isinstance(
        refusal_reason,
        str,
    ):
        raise ValueError(
            "refusal_reason 必须是字符串或 null"
        )

    timings = data.get("timings")

    if timings is not None:
        if not isinstance(timings, dict):
            raise ValueError("timings 必须是对象或 null")

        for field in TIMING_FIELDS:
            value = timings.get(field)

            if value is None:
                continue

            if (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or value < 0
            ):
                raise ValueError(
                    f"timings.{field} 必须是非负数或 null"
                )

    return data


def evaluate_case(
    endpoint: str,
    case: dict,
    *,
    timeout_seconds: float,
    post=requests.post,
) -> dict:
    started = time.perf_counter()

    try:
        response = post(
            endpoint,
            json={"question": case["question"]},
            timeout=timeout_seconds,
        )
        response.raise_for_status()
        data = validate_response(response.json())
    except Exception as exc:
        return {
            "id": case["id"],
            "question": case["question"],
            "expected_answerable": case["answerable"],
            "status": "error",
            "error_type": type(exc).__name__,
            "error": str(exc),
            "latency_ms": round(
                (time.perf_counter() - started) * 1000,
                2,
            ),
        }

    latency_ms = round(
        (time.perf_counter() - started) * 1000,
        2,
    )
    answer = data["answer"]
    normalized_answer = normalize(answer)
    matched_facts = [
        fact
        for fact in case["required_facts"]
        if normalize(fact) in normalized_answer
    ]
    missing_facts = [
        fact
        for fact in case["required_facts"]
        if normalize(fact) not in normalized_answer
    ]

    expected_documents = sorted({
        source_name(evidence["document"])
        for evidence in case["expected_evidence"]
    })
    cited_documents = sorted({
        source_name(citation["source"])
        for citation in data["citations"]
    })
    expected_document_set = set(expected_documents)
    cited_document_set = set(cited_documents)
    matched_documents = sorted(
        expected_document_set & cited_document_set
    )
    unexpected_documents = sorted(
        cited_document_set - expected_document_set
    )

    return {
        "id": case["id"],
        "category": case.get("category"),
        "difficulty": case.get("difficulty"),
        "question": case["question"],
        "expected_answerable": case["answerable"],
        "predicted_answerable": data["answerable"],
        "status": "ok",
        "latency_ms": latency_ms,
        "timings": data.get("timings") or {},
        "answer": answer,
        "reference_answer": case["reference_answer"],
        "refusal_reason": data.get("refusal_reason"),
        "required_facts": case["required_facts"],
        "matched_facts": matched_facts,
        "missing_facts": missing_facts,
        "fact_complete": (
            not missing_facts
            if case["answerable"]
            else None
        ),
        "expected_documents": expected_documents,
        "cited_documents": cited_documents,
        "matched_documents": matched_documents,
        "unexpected_documents": unexpected_documents,
        "citation_source_hit": (
            bool(matched_documents)
            if case["answerable"]
            else None
        ),
    }


def percentile(
    values: list[float],
    quantile: float,
) -> float | None:
    if not values:
        return None

    if not 0 <= quantile <= 1:
        raise ValueError("quantile 必须在 0 到 1 之间")

    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile
    lower_index = math.floor(position)
    upper_index = math.ceil(position)

    if lower_index == upper_index:
        return round(ordered[lower_index], 2)

    weight = position - lower_index
    value = (
        ordered[lower_index] * (1 - weight)
        + ordered[upper_index] * weight
    )
    return round(value, 2)


def summarize_stage_latency(
    results: list[dict],
    field: str,
) -> dict:
    values = [
        float(value)
        for result in results
        if isinstance(result.get("timings"), dict)
        for value in [result["timings"].get(field)]
        if (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
        )
    ]

    return {
        "count": len(values),
        "mean_ms": (
            round(sum(values) / len(values), 2)
            if values else None
        ),
        "p50_ms": percentile(values, 0.50),
        "p95_ms": percentile(values, 0.95),
    }


def summarize(results: list[dict]) -> dict:
    successful = [
        result
        for result in results
        if result["status"] == "ok"
    ]
    answerable_results = [
        result
        for result in successful
        if result["expected_answerable"] is True
    ]
    unanswerable_results = [
        result
        for result in successful
        if result["expected_answerable"] is False
    ]
    latencies = [
        result["latency_ms"]
        for result in successful
    ]

    true_positive = sum(
        result["expected_answerable"] is True
        and result["predicted_answerable"] is True
        for result in successful
    )
    false_negative = sum(
        result["expected_answerable"] is True
        and result["predicted_answerable"] is False
        for result in successful
    )
    true_negative = sum(
        result["expected_answerable"] is False
        and result["predicted_answerable"] is False
        for result in successful
    )
    false_positive = sum(
        result["expected_answerable"] is False
        and result["predicted_answerable"] is True
        for result in successful
    )

    required_fact_total = sum(
        len(result["required_facts"])
        for result in answerable_results
    )
    matched_fact_total = sum(
        len(result["matched_facts"])
        for result in answerable_results
    )
    expected_document_total = sum(
        len(result["expected_documents"])
        for result in answerable_results
    )
    matched_document_total = sum(
        len(result["matched_documents"])
        for result in answerable_results
    )
    cited_document_total = sum(
        len(result["cited_documents"])
        for result in answerable_results
    )

    answerable_count = true_positive + false_negative
    unanswerable_count = true_negative + false_positive

    return {
        "total": len(results),
        "successful": len(successful),
        "errors": len(results) - len(successful),
        "answerable": len(answerable_results),
        "unanswerable": len(unanswerable_results),
        "answerability_accuracy": (
            round(
                (true_positive + true_negative)
                / len(successful),
                4,
            )
            if successful else None
        ),
        "answerable_accept_rate": (
            round(true_positive / answerable_count, 4)
            if answerable_count else None
        ),
        "unanswerable_rejection_rate": (
            round(true_negative / unanswerable_count, 4)
            if unanswerable_count else None
        ),
        "unanswerable_false_accept_rate": (
            round(false_positive / unanswerable_count, 4)
            if unanswerable_count else None
        ),
        "answerability_confusion": {
            "true_positive": true_positive,
            "false_negative": false_negative,
            "true_negative": true_negative,
            "false_positive": false_positive,
        },
        "required_fact_coverage": (
            round(
                matched_fact_total / required_fact_total,
                4,
            )
            if required_fact_total else None
        ),
        "fact_complete_rate": (
            round(
                sum(
                    result["fact_complete"] is True
                    for result in answerable_results
                )
                / len(answerable_results),
                4,
            )
            if answerable_results else None
        ),
        "citation_source_precision": (
            round(
                matched_document_total
                / cited_document_total,
                4,
            )
            if cited_document_total else None
        ),
        "citation_source_recall": (
            round(
                matched_document_total
                / expected_document_total,
                4,
            )
            if expected_document_total else None
        ),
        "citation_case_hit_rate": (
            round(
                sum(
                    result["citation_source_hit"] is True
                    for result in answerable_results
                )
                / len(answerable_results),
                4,
            )
            if answerable_results else None
        ),
        "unanswerable_citation_free_rate": (
            round(
                sum(
                    not result["cited_documents"]
                    for result in unanswerable_results
                )
                / len(unanswerable_results),
                4,
            )
            if unanswerable_results else None
        ),
        "mean_latency_ms": (
            round(sum(latencies) / len(latencies), 2)
            if latencies else None
        ),
        "p50_latency_ms": percentile(latencies, 0.50),
        "p95_latency_ms": percentile(latencies, 0.95),
        "stage_latency": {
            field: summarize_stage_latency(
                successful,
                field,
            )
            for field in TIMING_FIELDS
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="通过 /api/qa 运行答案级评估"
    )
    parser.add_argument(
        "--dataset",
        type=Path,
        required=True,
        help="答案评估 JSONL 数据集",
    )
    parser.add_argument(
        "--endpoint",
        default="http://127.0.0.1:8000/api/qa",
        help="知识库问答接口地址",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=120.0,
        help="单次请求超时秒数",
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="只运行前 N 条，用于冒烟测试",
    )
    parser.add_argument(
        "--delay-ms",
        type=float,
        default=0.0,
        help="相邻请求之间的等待毫秒数",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="保存完整 JSON 报告",
    )
    args = parser.parse_args()

    if args.timeout <= 0:
        parser.error("timeout 必须大于 0")
    if args.limit is not None and args.limit < 1:
        parser.error("limit 必须大于 0")
    if args.delay_ms < 0:
        parser.error("delay-ms 不能小于 0")

    cases = load_dataset(args.dataset)
    if args.limit is not None:
        cases = cases[: args.limit]

    results: list[dict] = []

    for index, case in enumerate(cases, start=1):
        print(
            f"[{index}/{len(cases)}] {case['id']}",
            file=sys.stderr,
            flush=True,
        )
        results.append(
            evaluate_case(
                args.endpoint,
                case,
                timeout_seconds=args.timeout,
            )
        )

        if args.delay_ms and index < len(cases):
            time.sleep(args.delay_ms / 1000)

    report = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "config": {
            "dataset": str(args.dataset),
            "endpoint": args.endpoint,
            "timeout_seconds": args.timeout,
            "limit": args.limit,
            "delay_ms": args.delay_ms,
        },
        "summary": summarize(results),
        "results": results,
    }

    print(
        json.dumps(
            report["summary"],
            ensure_ascii=False,
            indent=2,
        )
    )

    if args.output:
        args.output.parent.mkdir(
            parents=True,
            exist_ok=True,
        )
        args.output.write_text(
            json.dumps(
                report,
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"报告已保存：{args.output.resolve()}")

    return 1 if report["summary"]["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
