"""Unified offline/replay/live entry point; live model calls require opt-in."""
import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.app.evaluation.offline import evaluate_parsing, evaluate_security_unit, read_performance_report
from backend.app.evaluation.reporting import LAYERS, compare_baseline, quality_layers, skipped, write_report
from backend.app.evaluation.runner import evaluate_local, replay
from backend.app.evaluation.http_runner import evaluate_conversations, evaluate_permissions
from scripts.evaluate_answers import load_dataset


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("offline", "replay", "live"), default="offline")
    parser.add_argument("--layers", default="all", help="all or comma-separated layer names")
    parser.add_argument("--dataset-dir", type=Path, default=ROOT / "evals/xinghai_v3")
    parser.add_argument("--corpus", type=Path, default=ROOT / "output/pdf/xinghai-department-corpus-v3")
    parser.add_argument("--config", type=Path, default=ROOT / "evals/system_config.example.json")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--trace-input", type=Path)
    parser.add_argument("--performance-report", type=Path)
    parser.add_argument("--baseline-report", type=Path, help="Compare a like-for-like previous system report")
    parser.add_argument("--limit", type=int, help="Limit each case collection, not total model calls")
    parser.add_argument("--case-ids", nargs='+', help="Only these single-turn IDs; applied before --limit")
    parser.add_argument("--conversation-ids", nargs='+', help="Only these conversation group IDs; applied before --limit")
    parser.add_argument("--ocr", action="store_true", help="Run real local OCR if runtime is available")
    parser.add_argument("--allow-model-calls", action="store_true")
    parser.add_argument("--allow-conversation-writes", action="store_true")
    parser.add_argument("--save-traces", action="store_true", help="Save sensitive authorized source/context snapshots locally")
    parser.add_argument("--strict", action="store_true", help="Exit 2 if requested checks are incomplete")
    args = parser.parse_args(argv)
    args.layers = list(LAYERS) if args.layers == "all" else list(dict.fromkeys(args.layers.split(",")))
    if not args.layers or any(layer not in LAYERS for layer in args.layers):
        parser.error("Unknown layer; choose: " + ",".join(LAYERS))
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be positive")
    if args.mode == "replay" and not args.trace_input:
        parser.error("--mode replay requires --trace-input")
    live_model_layers = {"retrieval", "rerank", "context", "answer", "conversations", "security"}
    if args.mode == "live" and live_model_layers.intersection(args.layers) and not args.allow_model_calls:
        parser.error("Live embedding/reranking/answer calls require --allow-model-calls")
    return args


def main(argv=None):
    args = parse_args(argv)
    directory = args.output_dir or ROOT / "evals/reports/system" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    directory.mkdir(parents=True, exist_ok=True)
    if (directory / "report.json").exists() or (directory / "report.md").exists():
        raise ValueError("Choose a new output directory; previous reports are not overwritten")
    cases = load_dataset(args.dataset_dir / "single_turn.jsonl")
    full_case_count = len(cases)
    if args.case_ids:
        unknown = set(args.case_ids) - {case['id'] for case in cases}
        if unknown:
            raise ValueError('Unknown single-turn IDs: ' + ','.join(sorted(unknown)))
        cases = [case for case in cases if case['id'] in args.case_ids]
    if args.limit:
        cases = cases[:args.limit]
    report = dict(schema_version=1, mode=args.mode, selected_cases=len(cases), available_cases=full_case_count,
                  selected_case_ids=[case["id"] for case in cases],
                  requested_conversation_ids=args.conversation_ids,
                  corpus_manifest_sha256=hashlib.sha256((args.corpus / "manifest.json").read_bytes()).hexdigest()
                      if (args.corpus / "manifest.json").is_file() else None,
                  scope_config_sha256=None,
                  dataset_sha256=hashlib.sha256((args.dataset_dir / "single_turn.jsonl").read_bytes()).hexdigest(),
                  live_model_calls_enabled=args.mode == "live" and args.allow_model_calls,
                  business_permission_changes=False, layers={name: skipped("not_selected") for name in args.layers})
    def run_layer(name, operation):
        print("Evaluating " + name, file=sys.stderr, flush=True)
        try:
            report["layers"][name] = operation()
        except Exception as exc:
            # Exceptions may contain database URLs or HTTP headers. Never write
            # exception strings/tracebacks from credentialed evaluation to reports.
            report["layers"][name] = dict(status="error", error_type=type(exc).__name__)
    if "parsing" in args.layers:
        run_layer("parsing", lambda: evaluate_parsing(args.corpus, cases, ocr=args.ocr))
    quality_requested = {"retrieval", "rerank", "context", "answer"}.intersection(args.layers)
    if args.mode == "replay" and quality_requested:
        quality = {}
        run_layer("_quality", lambda: replay(cases, args.trace_input, include_answer="answer" in quality_requested))
        quality = report["layers"].pop("_quality")
        if quality.get("status") == "error":
            report["layers"].update({name: quality for name in quality_requested})
        else:
            report["layers"].update({name: value for name, value in quality_layers(quality).items() if name in quality_requested})
    elif args.mode == "live":
        config = json.loads(args.config.read_text(encoding="utf-8-sig"))
        report["scope_config_sha256"] = hashlib.sha256(json.dumps(
            {key: config.get(key) for key in ("base_url", "knowledge_bases", "profiles")},
            sort_keys=True).encode()).hexdigest()
        if quality_requested or "index" in args.layers:
            run_layer("_quality", lambda: evaluate_local(cases if quality_requested else [], config,
                        include_answer="answer" in quality_requested,
                        include_index="index" in args.layers,
                        expected_documents=json.loads((args.corpus / "manifest.json").read_text(encoding="utf-8"))["documents"]
                            if "index" in args.layers else (),
                        save_traces=directory / "traces.jsonl" if args.save_traces else None))
            quality = report["layers"].pop("_quality")
            if quality.get("status") == "error":
                report["layers"].update({name: quality for name in quality_requested | ({"index"} if "index" in args.layers else set())})
            else:
                report["runtime"] = quality.get("runtime")
                report["layers"].update({name: value for name, value in quality_layers(quality).items() if name in quality_requested})
                if "index" in args.layers:
                    report["layers"]["index"] = quality["index"]
        if "conversations" in args.layers:
            if args.allow_conversation_writes:
                sessions = json.loads((args.dataset_dir / "conversations.json").read_text(encoding="utf-8"))
                if args.conversation_ids:
                    unknown = set(args.conversation_ids) - {row['id'] for row in sessions}
                    if unknown:
                        raise ValueError('Unknown conversation group IDs: ' + ','.join(sorted(unknown)))
                    sessions = [row for row in sessions if row['id'] in args.conversation_ids]
                run_layer("conversations", lambda: evaluate_conversations(sessions[:args.limit] if args.limit else sessions, config))
            else:
                report["layers"]["conversations"] = skipped("requires_allow_conversation_writes")
        if "security" in args.layers:
            permission = json.loads((args.dataset_dir / "permissions.json").read_text(encoding="utf-8"))
            if args.limit:
                permission["cases"] = permission["cases"][:args.limit]
            run_layer("security", lambda: evaluate_permissions(permission, config))
    else:
        for name in quality_requested:
            report["layers"][name] = skipped("requires_live_opt_in_or_actual_trace_replay")
    if "index" in args.layers and args.mode != "live":
        report["layers"]["index"] = skipped("requires_authenticated_read_only_database_access")
    if "conversations" in args.layers and args.mode != "live":
        report["layers"]["conversations"] = skipped("requires_real_http_session_or_separate_unit_tests")
    if "security" in args.layers and args.mode != "live":
        run_layer("security", lambda: evaluate_security_unit(ROOT, directory))
        report["layers"]["security"]["limitation"] = "Isolated mocked/SQLite regressions only; deployed-profile ACL tests have not run."
    if "performance" in args.layers:
        run_layer("performance", lambda: read_performance_report(args.performance_report))
    if args.baseline_report:
        baseline = json.loads(args.baseline_report.read_text(encoding="utf-8"))
        report["baseline_comparison"] = compare_baseline(report, baseline)
    write_report(report, directory)
    print(json.dumps(dict(overall_status=report["overall_status"], report_directory=str(directory)), ensure_ascii=True))
    if report["overall_status"] == "failed":
        return 1
    return 2 if args.strict and report["overall_status"] == "incomplete" else 0


if __name__ == "__main__":
    raise SystemExit(main())
