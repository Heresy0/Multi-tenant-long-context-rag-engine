import argparse
import json
import sys

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.load_test_support import (
    LoadTestThresholds,
    build_load_test_report,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="汇总 Locust CSV 并校验性能基线。"
    )
    parser.add_argument(
        "--stats",
        type=Path,
        required=True,
        help="Locust 生成的 *_stats.csv。",
    )
    parser.add_argument(
        "--failures",
        type=Path,
        help="Locust 生成的 *_failures.csv。",
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="汇总 JSON 输出路径。",
    )
    parser.add_argument(
        "--scenario",
        choices=("steady", "governance"),
        default="steady",
    )
    parser.add_argument("--min-requests", type=int, default=1)
    parser.add_argument(
        "--min-successful-requests",
        type=int,
        default=1,
    )
    parser.add_argument(
        "--max-failure-rate",
        type=float,
        default=0.01,
    )
    parser.add_argument(
        "--max-p95-ms",
        type=float,
        default=15_000,
    )
    parser.add_argument(
        "--max-server-errors",
        type=int,
        default=0,
    )
    parser.add_argument(
        "--min-throttled-requests",
        type=int,
        default=0,
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    numeric_values = {
        "min_requests": args.min_requests,
        "min_successful_requests": (
            args.min_successful_requests
        ),
        "max_failure_rate": args.max_failure_rate,
        "max_p95_ms": args.max_p95_ms,
        "max_server_errors": args.max_server_errors,
        "min_throttled_requests": (
            args.min_throttled_requests
        ),
    }
    if any(value < 0 for value in numeric_values.values()):
        raise SystemExit("阈值参数不能小于 0")
    if args.max_failure_rate > 1:
        raise SystemExit(
            "max-failure-rate 必须介于 0 和 1 之间"
        )

    report = build_load_test_report(
        stats_path=args.stats.resolve(),
        failures_path=(
            args.failures.resolve()
            if args.failures is not None
            else None
        ),
        thresholds=LoadTestThresholds(**numeric_values),
        scenario=args.scenario,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"报告已保存：{args.output.resolve()}")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
