"""Offline checks of the annotated pack; no models, HTTP or business DB."""
import hashlib
import json
from pathlib import Path

import pytest

from scripts.build_xinghai_evaluation_set import ANNOTATION_VERSION, CORPUS, GROUPS, OUTPUT, build
from scripts.evaluate_answers import load_dataset


@pytest.fixture(scope="module")
def generated(tmp_path_factory):
    if not (CORPUS / "manifest.json").exists():
        pytest.skip("Local synthetic corpus is required for splitter validation")
    folder = tmp_path_factory.mktemp("xinghai-pack")
    build(CORPUS, folder)
    return folder


@pytest.fixture(scope="module")
def cases():
    return {case["id"]: case for case in load_dataset(OUTPUT / "single_turn.jsonl")}


def test_counts_scopes_and_revision(cases):
    assert len(cases) == 60
    assert sum(case["answerable"] for case in cases.values()) == 53
    assert all(case["annotation_version"] == ANNOTATION_VERSION for case in cases.values())
    assert all(case["manual_review_required"] and case["semantic_checks"] for case in cases.values())
    for group in GROUPS:
        scoped = load_dataset(OUTPUT / "by_scope" / f"{group}.jsonl")
        assert scoped == [case for case in cases.values() if case["source_groups"] == [group]]
    cross = load_dataset(OUTPUT / "cross_scope.jsonl")
    assert cross == [case for case in cases.values() if len(case["source_groups"]) > 1]
    negative = load_dataset(OUTPUT / "unanswerable.jsonl")
    assert negative == [case for case in cases.values() if not case["answerable"]]
    assert len(negative) == 7
    assert len({item["document"] for case in cases.values() for item in case["expected_evidence"]}) == 30


@pytest.mark.parametrize("identifier,core", [
    ("XH-001", ["2次"]), ("XH-002", ["5个工作日"]),
    ("XH-003", ["650元"]), ("XH-016", ["550元"]),
    ("XH-018", ["证书管理", "每月"]),
    ("XH-D-25", ["HR", "最后工作日前", "2个工作日"]),
    ("XH-D-30", ["6次", "7日"]), ("XH-E-048", ["不能"]),
])
def test_core_matches_question_without_forced_extras(cases, identifier, core):
    assert cases[identifier]["required_facts"] == core
    assert cases[identifier]["optional_facts"]


def test_roles_conditions_and_status_are_reviewed(cases):
    assert "跨部门审批代理" in cases["XH-031"]["required_facts"]
    assert "书面反馈" in cases["XH-D-22"]["required_facts"]
    assert "组合" in cases["XH-E-050"]["required_facts"]
    assert "不阻断核心流程" in cases["XH-E-055"]["required_facts"]
    assert "可接受替代方案" in cases["XH-E-055"]["required_facts"]
    assert "不同" in cases["XH-E-056"]["required_facts"]
    assert "不可交换" in cases["XH-028"]["semantic_checks"][0]
    for identifier in ("XH-022", "XH-D-26"):
        assert cases[identifier]["forbidden_claims"] == ["DEF-026-08已经修复"]
        assert len(cases[identifier]["accepted_source_document_sets"]) == 2
        assert len(cases[identifier]["required_source_documents"]) == 1


def test_version_and_scan_regressions_are_not_removed(cases):
    sessions = json.loads((OUTPUT / "conversations.json").read_text(encoding="utf-8"))
    assert len(sessions) == 8 and sum(len(s["turns"]) for s in sessions) == 24
    by_id = {s["id"]: s for s in sessions}
    versions = by_id["XH-MT-02"]["turns"]
    assert "哪个版本" in versions[0]["question"]
    assert versions[0]["required_facts"] == ["650元", "1.4"]
    assert versions[1]["question"] == "2025年呢？"
    assert versions[1]["required_facts"] == ["550元", "1.2"]
    assert by_id["XH-MT-06"]["turns"][2]["required_facts"] == ["部门负责人", "HR负责人", "财务支持组"]
    assert by_id["XH-MT-07"]["turns"][2]["required_facts"] == ["不能"]
    scan = cases["XH-E-057"]
    assert scan["required_facts"] == ["林澈", "周宁"]
    assert "周末" in scan["known_issue"]
    assert "仅依据" in scan["question"]
    assert len(scan["required_source_documents"]) == 1
    # Alternatives apply only to the all-access maintenance conversation.
    assert len(by_id["XH-MT-03"]["turns"][0]["accepted_source_document_sets"]) == 3


def test_permissions_and_manual_only_boundaries():
    permission = json.loads((OUTPUT / "permissions.json").read_text(encoding="utf-8"))
    assert len(permission["cases"]) == 12
    assert permission["annotation_version"] == ANNOTATION_VERSION
    assert all(c["manual_review_required"] for c in permission["cases"])
    assert all(next(c for c in permission["cases"] if c["id"] == identifier)["preconditions"]
               for identifier in ("ACL-08", "ACL-10", "ACL-11", "ACL-12"))
    guide = (OUTPUT / "README.md").read_text(encoding="utf-8")
    assert "现有评分代码不会自动执行" in guide
    assert "不能充当" in guide and "Alice" in guide
    assert "旧报告仍对应旧标注" in guide


def pack_hashes(folder):
    return {str(path.relative_to(folder)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in folder.rglob("*") if path.is_file()}


def test_regeneration_matches_delivered_pack_and_current_splitters(generated):
    assert pack_hashes(generated) == pack_hashes(OUTPUT)
    coverage = json.loads((generated / "coverage.json").read_text(encoding="utf-8"))
    assert coverage["source_hashes"] == coverage["structural_validation"] == "passed"
    assert coverage["external_model_calls"] == coverage["database_writes"] == 0
    assert coverage["live_evaluation_executed"] is False
    assert all(item["found"] for item in coverage["evidence_checks"])
    assert {item["method"] for item in coverage["evidence_checks"]} == {
        "current_project_splitter_chunk_with_current_file_hash",
        "source_scan_ground_truth_not_ocr_accuracy",
    }


def test_output_protection_and_explicit_refresh(generated):
    before = pack_hashes(generated)
    with pytest.raises(FileExistsError):
        build(CORPUS, generated)
    assert pack_hashes(generated) == before
    # Copy an existing test source as an unknown file, not a generated target.
    import shutil
    sentinel = generated / "review-notes.py"
    shutil.copyfile(Path(__file__), sentinel)
    digest = hashlib.sha256(sentinel.read_bytes()).hexdigest()
    build(CORPUS, generated, refresh=True)
    assert hashlib.sha256(sentinel.read_bytes()).hexdigest() == digest
    assert {key: value for key, value in pack_hashes(generated).items() if key != sentinel.name} == before
