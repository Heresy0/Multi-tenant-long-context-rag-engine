import argparse
import json
import math
import sys
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

# 支持从项目根目录执行：
# python scripts/inspect_retrieval.py
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))


def normalize(text: str) -> str:
    """统一字符形式并忽略空白，方便匹配原文证据。"""
    return "".join(
        unicodedata.normalize("NFKC", text).split()
    )


def source_name(source: str) -> str:
    """同时兼容 Windows 和 Linux 路径。"""
    return source.replace("\\", "/").rsplit("/", 1)[-1]


def load_dataset(path: Path) -> list[dict]:
    cases = []
    seen_ids = set()

    with path.open("r", encoding="utf-8-sig") as file:
        for line_number, line in enumerate(file, start=1):
            if not line.strip():
                continue

            case = json.loads(line)

            if not isinstance(case, dict):
                raise ValueError(f"第 {line_number} 行必须是 JSON 对象")

            for field in ("id", "question"):
                value = case.get(field)
                if not isinstance(value, str) or not value.strip():
                    raise ValueError(
                        f"第 {line_number} 行缺少有效的 {field}"
                    )

            if case["id"] in seen_ids:
                raise ValueError(f"样本 ID 重复：{case['id']}")

            if type(case.get("answerable")) is not bool:
                raise ValueError(
                    f"{case['id']} 的 answerable 必须是布尔值"
                )

            evidence = case.get("expected_evidence")
            if not isinstance(evidence, list):
                raise ValueError(
                    f"{case['id']} 的 expected_evidence 必须是列表"
                )

            if case["answerable"] and not evidence:
                raise ValueError(
                    f"{case['id']} 可以回答，但没有标注证据"
                )

            if not case["answerable"] and evidence:
                raise ValueError(
                    f"{case['id']} 不可回答，不应填写正确证据"
                )

            for item in evidence:
                if not isinstance(item, dict) or not all(
                    isinstance(item.get(key), str)
                    and item[key].strip()
                    for key in ("document", "text")
                ):
                    raise ValueError(
                        f"{case['id']} 的证据缺少 document 或 text"
                    )

            seen_ids.add(case["id"])
            cases.append(case)

    if not cases:
        raise ValueError("题集为空")

    return cases


def matches_evidence(document, evidence: dict) -> bool:
    """要求来源文件相同，且分块包含标注的原文证据。"""
    actual_source = source_name(
        str(document.metadata.get("source", ""))
    )
    expected_source = source_name(evidence["document"])

    return (
        actual_source == expected_source
        and normalize(evidence["text"])
        in normalize(document.page_content)
    )


def evaluate_case(
    retriever,
    case: dict,
    k: int,
    answerability_threshold: float | None = None,
) -> dict:
    started = time.perf_counter()

    try:
        documents = retriever.invoke(case["question"])[:k]
    except Exception as exc:
        # 接口失败与检索未命中分开记录。
        return {
            "id": case["id"],
            "question": case["question"],
            "answerable": case["answerable"],
            "status": "error",
            "error_type": type(exc).__name__,
            "latency_ms": round(
                (time.perf_counter() - started) * 1000, 2
            ),
        }

    latency_ms = round(
        (time.perf_counter() - started) * 1000, 2
    )

    first_metadata = (
        documents[0].metadata if documents else {}
    )
    top_rerank_score = first_metadata.get("rerank_score")

    predicted_answerable = None

    if answerability_threshold is not None:
        if not documents:
            predicted_answerable = False
        elif isinstance(top_rerank_score, (int, float)):
            predicted_answerable = (
                float(top_rerank_score)
                >= answerability_threshold
            )

    hits = []
    first_relevant_rank = None

    for rank, document in enumerate(documents, start=1):
        relevant = None

        # 单题调试和无答案样本不自动判定相关性。
        if case["answerable"] is True:
            relevant = any(
                matches_evidence(document, evidence)
                for evidence in case["expected_evidence"]
            )

            if relevant and first_relevant_rank is None:
                first_relevant_rank = rank

        hits.append({
            "rank": rank,
            "chunk_id": (
                getattr(document, "id", None)
                or document.metadata.get("chunk_id")
            ),
            "source": document.metadata.get("source"),
            "page": document.metadata.get("page"),
            "start_index": document.metadata.get("start_index"),
            "rrf_rank": document.metadata.get("rrf_rank"),
            "rrf_score": document.metadata.get("rrf_score"),
            "rerank_score": document.metadata.get("rerank_score"),
            "rerank_status": document.metadata.get("rerank_status"),
            "relevant": relevant,
            "content": document.page_content,
        })

    scored = case["answerable"] is True

    return {
        "id": case["id"],
        "question": case["question"],
        "answerable": case["answerable"],
        "status": "ok",
        "latency_ms": latency_ms,
        "retrieval_latency_ms": first_metadata.get(
            "retrieval_latency_ms"
        ),
        "rerank_latency_ms": first_metadata.get(
            "rerank_latency_ms"
        ),
        "rerank_candidate_count": first_metadata.get(
            "rerank_candidate_count"
        ),
        "top_rerank_score": top_rerank_score,
        "predicted_answerable": predicted_answerable,
        "hit_at_k": (
            first_relevant_rank is not None if scored else None
        ),
        "first_relevant_rank": first_relevant_rank,
        "reciprocal_rank": (
            (1 / first_relevant_rank if first_relevant_rank else 0)
            if scored else None
        ),
        "hits": hits,
    }


def percentile(
    values: list[float],
    quantile: float,
) -> float | None:
    """使用线性插值计算分位数。"""
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
    interpolated = (
        ordered[lower_index] * (1 - weight)
        + ordered[upper_index] * weight
    )

    return round(interpolated, 2)


def summarize(results: list[dict], k: int) -> dict:
    successful = [
        result for result in results
        if result["status"] == "ok"
    ]
    scored = [
        result for result in successful
        if result["answerable"] is True
    ]
    latencies = [
        result["latency_ms"]
        for result in successful
    ]
    retrieval_latencies = [
        result["retrieval_latency_ms"]
        for result in successful
        if isinstance(
            result.get("retrieval_latency_ms"),
            (int, float),
        )
    ]
    rerank_latencies = [
        result["rerank_latency_ms"]
        for result in successful
        if isinstance(
            result.get("rerank_latency_ms"),
            (int, float),
        )
    ]
    answerability_scored = [
        result
        for result in successful
        if type(result.get("answerable")) is bool
        and type(result.get("predicted_answerable")) is bool
    ]
    true_positive = sum(
        result["answerable"] is True
        and result["predicted_answerable"] is True
        for result in answerability_scored
    )
    false_negative = sum(
        result["answerable"] is True
        and result["predicted_answerable"] is False
        for result in answerability_scored
    )
    true_negative = sum(
        result["answerable"] is False
        and result["predicted_answerable"] is False
        for result in answerability_scored
    )
    false_positive = sum(
        result["answerable"] is False
        and result["predicted_answerable"] is True
        for result in answerability_scored
    )
    answerable_predictions = true_positive + false_negative
    unanswerable_predictions = true_negative + false_positive

    return {
        "total": len(results),
        "successful": len(successful),
        "errors": len(results) - len(successful),
        "scored_answerable": len(scored),
        "unanswerable": sum(
            result["answerable"] is False for result in results
        ),
        f"hit_rate_at_{k}": (
            round(
                sum(result["hit_at_k"] for result in scored)
                / len(scored),
                4,
            )
            if scored else None
        ),
        f"mrr_at_{k}": (
            round(
                sum(result["reciprocal_rank"] for result in scored)
                / len(scored),
                4,
            )
            if scored else None
        ),
        "mean_latency_ms": (
            round(
                sum(latencies) / len(latencies),
                2,
            )
            if latencies else None
        ),
        "p50_latency_ms": percentile(latencies, 0.50),
        "p95_latency_ms": percentile(latencies, 0.95),
        "mean_retrieval_latency_ms": (
            round(
                sum(retrieval_latencies)
                / len(retrieval_latencies),
                2,
            )
            if retrieval_latencies else None
        ),
        "p50_retrieval_latency_ms": percentile(
            retrieval_latencies,
            0.50,
        ),
        "p95_retrieval_latency_ms": percentile(
            retrieval_latencies,
            0.95,
        ),
        "mean_rerank_latency_ms": (
            round(
                sum(rerank_latencies)
                / len(rerank_latencies),
                2,
            )
            if rerank_latencies else None
        ),
        "p50_rerank_latency_ms": percentile(
            rerank_latencies,
            0.50,
        ),
        "p95_rerank_latency_ms": percentile(
            rerank_latencies,
            0.95,
        ),
        "answerability_scored": len(answerability_scored),
        "answerability_accuracy": (
            round(
                (true_positive + true_negative)
                / len(answerability_scored),
                4,
            )
            if answerability_scored else None
        ),
        "answerable_accept_rate": (
            round(
                true_positive / answerable_predictions,
                4,
            )
            if answerable_predictions else None
        ),
        "unanswerable_rejection_rate": (
            round(
                true_negative / unanswerable_predictions,
                4,
            )
            if unanswerable_predictions else None
        ),
        "unanswerable_false_accept_rate": (
            round(
                false_positive / unanswerable_predictions,
                4,
            )
            if unanswerable_predictions else None
        ),
        "answerability_confusion": {
            "true_positive": true_positive,
            "false_negative": false_negative,
            "true_negative": true_negative,
            "false_positive": false_positive,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="当前 RAG 检索基线：单题调试或批量评估"
    )
    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument("--question", help="单个检索问题")
    inputs.add_argument("--dataset", type=Path, help="JSONL 题集")
    parser.add_argument("--k", type=int, default=8)
    parser.add_argument("--fetch-k", type=int, default=30)
    parser.add_argument(
        "--answerability-threshold",
        type=float,
        default=0.70,
        help=(
            "Top-1 重排分数低于该值时预测为无答案"
        ),
    )
    parser.add_argument("--output", type=Path, help="保存 JSON 报告")
    args = parser.parse_args()

    if args.k < 1 or args.fetch_k < args.k:
        parser.error("需要满足 fetch-k >= k >= 1")

    if not 0 <= args.answerability_threshold <= 1:
        parser.error("answerability-threshold 必须在 0 到 1 之间")

    if args.question is not None and not args.question.strip():
        parser.error("问题不能为空")

    if args.dataset:
        cases = load_dataset(args.dataset)
    else:
        cases = [{
            "id": "manual",
            "question": args.question,
            "answerable": None,
            "expected_evidence": [],
        }]

    # 延迟导入：查看 --help 时不初始化模型或向量库。
    from backend.app.rag import create_retriever

    # 参数在检索器创建时固定，避免评估过程中修改
    # 共享检索器的运行时状态。
    retriever = create_retriever(
        k=args.k,
        fetch_k=args.fetch_k,
    )

    results = [
        evaluate_case(
            retriever,
            case,
            args.k,
            args.answerability_threshold,
        )
        for case in cases
    ]

    report = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "config": {
            "search_type": retriever.search_type,
            "search_kwargs": dict(retriever.search_kwargs),
            "answerability_threshold": (
                args.answerability_threshold
            ),
            "dataset": str(args.dataset) if args.dataset else None,
        },
        "summary": summarize(results, args.k),
        "results": results,
    }

    # 单题显示完整片段；批量运行在终端只显示汇总。
    console_data = report if args.question is not None else report["summary"]
    print(json.dumps(console_data, ensure_ascii=False, indent=2))

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"报告已保存：{args.output.resolve()}")

    # 有请求失败时返回非零退出码，避免被误认为完整成功。
    return 1 if report["summary"]["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
