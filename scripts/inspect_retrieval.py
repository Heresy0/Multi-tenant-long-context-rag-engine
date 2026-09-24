import argparse
import json
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


def evaluate_case(retriever, case: dict, k: int) -> dict:
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


def summarize(results: list[dict], k: int) -> dict:
    successful = [
        result for result in results
        if result["status"] == "ok"
    ]
    scored = [
        result for result in successful
        if result["answerable"] is True
    ]

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
                sum(result["latency_ms"] for result in successful)
                / len(successful),
                2,
            )
            if successful else None
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="当前 RAG 检索基线：单题调试或批量评估"
    )
    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument("--question", help="单个检索问题")
    inputs.add_argument("--dataset", type=Path, help="JSONL 题集")
    parser.add_argument("--k", type=int, default=3)
    parser.add_argument("--fetch-k", type=int, default=15)
    parser.add_argument("--output", type=Path, help="保存 JSON 报告")
    args = parser.parse_args()

    if args.k < 1 or args.fetch_k < args.k:
        parser.error("需要满足 fetch-k >= k >= 1")

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
        evaluate_case(retriever, case, args.k)
        for case in cases
    ]

    report = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "config": {
            "search_type": retriever.search_type,
            "search_kwargs": dict(retriever.search_kwargs),
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
