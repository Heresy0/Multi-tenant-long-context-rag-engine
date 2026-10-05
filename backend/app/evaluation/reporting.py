"""Unified reports explicitly distinguish untested stages from passing checks."""
import json
from datetime import datetime, timezone


LAYERS = ("parsing", "index", "retrieval", "rerank", "context", "answer", "conversations", "security", "performance")


def skipped(reason):
    return dict(status="skipped", reason=reason)


def stage_report(quality, stage):
    records = []
    for record in quality.get("records", []):
        metric = record.get("metrics", {}).get(stage)
        if record["status"] == "error":
            records.append(dict(id=record["id"], status="error", error_type=record.get("error_type")))
        elif metric is not None and metric.get("reason") == "no_positive_evidence_labels":
            records.append(dict(id=record["id"], status="not_applicable", reason="negative_case_has_no_positive_retrieval_labels"))
        elif metric is None or not metric.get("scored"):
            records.append(dict(id=record["id"], status="skipped", reason="no_observation_or_positive_labels"))
        else:
            records.append(dict(id=record["id"], status="passed" if metric["all_evidence_present"] else "failed", metrics=metric))
    from .metrics import summarize_records
    return dict(status="completed", summary=summarize_records(records), records=records,
                metrics=quality.get("stages", {}).get(stage), evaluation_type=quality.get("evaluation_type"))


def quality_layers(quality):
    answer_records = []
    for record in quality.get("records", []):
        metric = record.get("metrics", {}).get("answer")
        status = "error" if record["status"] == "error" else "skipped" if metric is None else "passed" if metric["automatic_proxy_pass"] else "failed"
        answer_records.append(dict(id=record["id"], status=status, metrics=metric,
                                   response=record.get("response"), error_type=record.get("error_type")))
    from .metrics import summarize_records
    return dict(
        retrieval=dict(status="completed", stages={key: quality.get("stages", {}).get(key) for key in ("vector", "keyword", "fusion")},
                       **{key: value for key, value in stage_report(quality, "fusion").items() if key != "status"}),
        rerank=stage_report(quality, "rerank"), context=stage_report(quality, "context"),
        answer=dict(status="completed", summary=summarize_records(answer_records), records=answer_records,
                    limitation="Phrase/source proxies; semantic correctness and faithfulness require human review."))


def outcome(report):
    incomplete = report.get("mode") == "replay"
    for layer in report["layers"].values():
        summary = layer.get("summary", {})
        if layer["status"] in ("failed", "error") or summary.get("failed", 0) or summary.get("error", 0):
            return "failed"
        if (layer["status"] in ("skipped", "partial") or summary.get("skipped", 0)
                or summary.get("partial", 0) or layer.get("evaluation_type", "").startswith("replay")):
            incomplete = True
        if layer.get("status") == "imported" and layer.get("report", {}).get("passed") is not True:
            return "failed"
    return "incomplete" if incomplete else "passed_automated_checks_not_production_certification"


def compare_baseline(report, baseline):
    """Only compare like-for-like runs; never turn historical Chroma numbers into current gates."""
    keys = ("schema_version", "mode", "dataset_sha256", "selected_case_ids", "requested_conversation_ids", "corpus_manifest_sha256", "scope_config_sha256")
    mismatches = [key for key in keys if report.get(key) != baseline.get(key)]
    if set(report["layers"]) != set(baseline.get("layers", {})):
        mismatches.append("selected_layers")
    if mismatches:
        return dict(status="incompatible", mismatches=mismatches)
    changes = []
    for name, layer in report["layers"].items():
        previous = baseline["layers"][name]
        metrics = layer.get("metrics", {}) or {}
        for metric in ("hit_rate", "mrr", "mean_evidence_recall", "all_evidence_rate"):
            current, old = metrics.get(metric), (previous.get("metrics", {}) or {}).get(metric)
            if isinstance(current, (int, float)) and isinstance(old, (int, float)):
                changes.append(dict(layer=name, metric=metric, baseline=old, current=current, delta=current - old))
        for metric in ("pass_rate",):
            current, old = layer.get("summary", {}).get(metric), previous.get("summary", {}).get(metric)
            if isinstance(current, (int, float)) and isinstance(old, (int, float)):
                changes.append(dict(layer=name, metric=metric, baseline=old, current=current, delta=current - old))
    return dict(status="compared", changes=changes, limitation="Deltas are diagnostic, not an automatic deployment gate.")


def write_report(report, directory):
    report["created_at"] = datetime.now(timezone.utc).isoformat()
    report["overall_status"] = outcome(report)
    (directory / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = ["# 企业知识库分层评估报告", "", "运行模式：" + report["mode"], "",
             "结论：" + report["overall_status"], "", "passed仅表示自动检查通过，不代表语义评审通过或生产安全认证。", "",
             "| 层 | 执行状态 | 通过 | 失败 | 错误 | 未测试/部分 | 不适用 |", "|---|---|---:|---:|---:|---:|---:|"]
    for name, layer in report["layers"].items():
        summary = layer.get("summary", {})
        lines.append(f"| {name} | {layer['status']} | {summary.get('passed', '—')} | {summary.get('failed', '—')} | {summary.get('error', '—')} | {summary.get('skipped', 0) + summary.get('partial', 0) if summary else '—'} | {summary.get('not_applicable', 0) if summary else '—'} |")
    for name, layer in report["layers"].items():
        lines.extend(["", "## " + name, ""])
        for key in ("reason", "limitation", "evaluation_type"):
            if layer.get(key):
                lines.extend([f"{key}：{layer[key]}", ""])
        metrics = layer.get("metrics") or layer.get("stages")
        if metrics:
            lines.extend(["```json", json.dumps(metrics, ensure_ascii=False, indent=2), "```", ""])
        flagged = [row for row in layer.get("records", []) if row["status"] not in ("passed", "not_applicable")]
        if flagged:
            lines.extend(["非通过项（完整结果见report.json）：", ""])
            lines.extend(f"- {row['id']}：{row['status']}；{row.get('reason', row.get('error_type', '请核对指标和原始结果'))}" for row in flagged)
    lines.extend(["", "## 解读限制", "",
                  "- 未测试不能算通过；不同范围、身份、语料版本的分数不可直接比较。",
                  "- 关键词匹配不能识别所有否定、错误归因或同义表达，需人工抽检。",
                  "- 端到端事实正确性、忠实上下文、引用支持是不同指标。",
                  "- 报告可能含业务答案；原始trace包含授权文档片段，请按内部敏感资料保管，不上传知识库。", ""])
    if report.get("baseline_comparison"):
        lines.extend(["## 同口径基线对比", "", "```json", json.dumps(report["baseline_comparison"], ensure_ascii=False, indent=2), "```", ""])
    (directory / "report.md").write_text("\n".join(lines), encoding="utf-8")
