"""Local parsing and isolated security regression checks; no business DB writes."""
import hashlib
import json
import subprocess
import sys
import xml.etree.ElementTree as ET

from .metrics import evidence_metrics, summarize_records


def evaluate_parsing(corpus, cases, *, ocr=False):
    from ..documents.document_splitter import split_docx
    from ..documents.pdf_splitter import PdfOcrUnavailableError, split_pdf
    from ..documents.text_splitter import split_markdown, split_txt
    manifest = json.loads((corpus / "manifest.json").read_text(encoding="utf-8"))
    chunks, records = {}, []
    expected_files = set()
    root = (corpus / "documents").resolve()
    for entry in manifest["documents"]:
        path = (corpus / entry["file"]).resolve()
        if not path.is_relative_to(root):
            raise ValueError("Manifest contains a path outside documents")
        expected_files.add(path)
        record = dict(id=path.name, group=entry["group"], format=entry["format"], kind="document")
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != entry["sha256"]:
            records.append(dict(record, status="failed", reason="file_missing_or_hash_changed"))
            continue
        if "OCR必需" in entry.get("features", []) and not ocr:
            records.append(dict(record, status="skipped", reason="real_ocr_not_enabled"))
            continue
        try:
            splitter = {"docx": split_docx, "pdf": split_pdf, "md": split_markdown, "txt": split_txt}[entry["format"]]
            parsed = splitter(path)
            chunks[path.name] = [dict(document_name=path.name, content=chunk.page_content) for chunk in parsed]
            # An introductory paragraph legitimately has an empty section path.
            valid = bool(parsed) and all(chunk.page_content.strip() and chunk.metadata.get("document_name")
                                        and "section_path" in chunk.metadata for chunk in parsed)
            records.append(dict(record, status="passed" if valid else "failed", chunks=len(parsed),
                                check="nonempty_chunks_and_document_section_fields"))
        except PdfOcrUnavailableError:
            records.append(dict(record, status="skipped", reason="ocr_runtime_unavailable"))
        except Exception as exc:
            records.append(dict(record, status="error", error_type=type(exc).__name__))
    actual_files = {path.resolve() for path in root.rglob("*") if path.is_file()}
    records.append(dict(id="manifest_inventory", kind="inventory",
                        status="passed" if actual_files == expected_files else "failed"))
    for case in cases:
        evidence = case["expected_evidence"]
        if not evidence:
            continue
        available = [item for item in evidence if item["document"] in chunks]
        rows = [chunk for name in {item["document"] for item in available} for chunk in chunks[name]]
        if not available:
            records.append(dict(id=case["id"], kind="evidence", status="skipped", reason="source_not_parsed"))
            continue
        metrics = evidence_metrics(available, rows)
        state = "failed" if not metrics["all_evidence_present"] else "passed" if len(available) == len(evidence) else "partial"
        records.append(dict(id=case["id"], kind="evidence", status=state, metrics=metrics,
                            missing_source_count=len(evidence) - len(available)))
    return dict(status="completed", evaluation_type="offline_actual_project_splitters",
                summary=summarize_records(records), records=records,
                limitations=["Anchors are selected evidence, not exhaustive parsing accuracy or retrieval recall."])


SECURITY_TEST_FILES = ["tests/test_authorization.py", "tests/test_qa_api.py",
                       "tests/test_conversations.py", "tests/test_oidc.py"]


def evaluate_security_unit(project_root, report_dir):
    # Test paths are a fixed allowlist, never arbitrary commands from a config.
    junit = (report_dir / "security_unit.xml").resolve()
    # Keep the load-test plugin/fixtures out of regular TestClient regressions.
    # The test host still needs local socketpair access for asyncio on Windows.
    command = [sys.executable, "-m", "pytest", "-p", "no:locust", *SECURITY_TEST_FILES, "-q", f"--junitxml={junit}"]
    try:
        result = subprocess.run(command, cwd=project_root, capture_output=True, timeout=180)
    except subprocess.TimeoutExpired:
        return dict(status="error", reason="isolated_security_tests_timed_out")
    if not junit.exists():
        return dict(status="error", reason="no_junit_result", exit_code=result.returncode)
    records = []
    for test in ET.parse(junit).iter("testcase"):
        status = "failed" if test.find("failure") is not None else "error" if test.find("error") is not None else "skipped" if test.find("skipped") is not None else "passed"
        records.append(dict(id=f"{test.get('classname')}::{test.get('name')}", status=status))
    return dict(status="completed" if result.returncode == 0 else "failed", exit_code=result.returncode,
                evaluation_type="isolated_unit_regression_not_deployed_acl_validation",
                summary=summarize_records(records), records=records)


def read_performance_report(path):
    if path is None:
        return dict(status="skipped", reason="no_locust_summary_supplied")
    report = json.loads(path.read_text(encoding="utf-8"))
    if "checks" not in report or "summary" not in report:
        raise ValueError("Expected a summarize_load_test.py report with summary and checks")
    if ("minimum_successful_requests" not in report["checks"]
            or "successful_request_count" not in report["summary"]):
        return dict(status="partial", reason="legacy_report_does_not_validate_successful_qa_requests",
                    source=str(path), report=report)
    return dict(status="imported", evaluation_type="existing_locust_report_not_a_new_load_test",
                source=str(path), report=report)
