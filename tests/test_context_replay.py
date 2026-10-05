import hashlib
import json

import pytest

from scripts.check_context_replay import check


def fixture_files(tmp_path):
    question = "项目最后何时验收？"
    case = dict(id="Q-1", question=question, reference_answer="3月8日签署最终验收。",
                answerable=True, required_facts=["3月8日"],
                expected_evidence=[dict(document="项目记录", text="签署最终验收")])
    rows = [dict(chunk_id=f"c-{index}", document_name="项目记录", document_id="project",
                 content=text, tenant_id="tenant", knowledge_base_id="kb")
            for index, text in enumerate([
                "原计划3月1日验收。", "参会人员记录。", "客户验收账号已完成。",
                "版本编号说明。", "客户于3月8日签署最终验收。",
            ])]
    trace = dict(id=case["id"], question_hash=hashlib.sha256(question.encode()).hexdigest(),
                 scope=dict(tenant_id="tenant", knowledge_base_ids=["kb"]),
                 stages=dict(authorized_final=rows, context=rows[:4]))
    dataset, snapshot = tmp_path / "dataset.jsonl", tmp_path / "trace.jsonl"
    dataset.write_text(json.dumps(case, ensure_ascii=False) + "\n", encoding="utf-8")
    snapshot.write_text(json.dumps(trace, ensure_ascii=False) + "\n", encoding="utf-8")
    return dataset, snapshot, trace


def test_replay_reconstructs_context_without_claiming_new_answer_accuracy(tmp_path):
    dataset, snapshot, _ = fixture_files(tmp_path)
    result = check(snapshot, dataset)
    assert result["scored"] == 1 and result["old_complete"] == 0 and result["new_complete"] == 1
    assert result["improved"] == ["Q-1"] and not result["regressed"]
    assert result["evaluation_type"] == "old_context_reconstruction_not_new_live_answers"
    assert "签署最终验收" not in json.dumps(result, ensure_ascii=False)


def test_replay_rejects_changed_question(tmp_path):
    dataset, snapshot, trace = fixture_files(tmp_path)
    trace["question_hash"] = hashlib.sha256("新的问题".encode()).hexdigest()
    snapshot.write_text(json.dumps(trace), encoding="utf-8")
    with pytest.raises(ValueError, match="question changed"):
        check(snapshot, dataset)


def test_replay_rejects_cross_tenant_kb_or_flagged_candidates(tmp_path):
    dataset, snapshot, trace = fixture_files(tmp_path)
    original = dict(trace["stages"]["authorized_final"][0])
    for change in (dict(tenant_id="other"), dict(knowledge_base_id="other"), dict(scope_violation=True)):
        trace["stages"]["authorized_final"][0] = dict(original, **change)
        snapshot.write_text(json.dumps(trace), encoding="utf-8")
        with pytest.raises(ValueError, match="out-of-scope"):
            check(snapshot, dataset)


def test_empty_and_duplicate_snapshots_cannot_look_like_passing_replay(tmp_path):
    dataset, snapshot, trace = fixture_files(tmp_path)
    snapshot.write_text("", encoding="utf-8")
    with pytest.raises(ValueError, match="No valid answerable traces"):
        check(snapshot, dataset)
    snapshot.write_text((json.dumps(trace) + "\n") * 2, encoding="utf-8")
    with pytest.raises(ValueError, match="Duplicate trace ID"):
        check(snapshot, dataset)
