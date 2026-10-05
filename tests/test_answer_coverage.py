"""Independent-domain fixtures, not an accuracy claim about a live model."""
import json

import pytest
from pydantic import ValidationError

from backend.app.knowledge.answer_coverage import coverage_errors, question_requirements, requirement_prompt
from backend.app.knowledge.answer_models import AnswerClaim, GenerationAnswerDraft


def draft(coverage, *, answerable=True, claims=None):
    return GenerationAnswerDraft(answerable=answerable,
                                 claims=claims if claims is not None else [AnswerClaim(text="答案", citations=["资料1"])],
                                 coverage=coverage)


def row(requirement_id="Q1", aspect="办理条件", **kwargs):
    return dict(dict(requirement_id=requirement_id, aspect=aspect, status="answered", claim_indices=[1],
                     missing_information=None), **kwargs)


@pytest.mark.parametrize("question,count", [
    ("采购由谁审批？紧急采购有什么例外？", 2),
    ("育儿假如何申请，以及兼职员工是否适用", 1),
    ("VPN为什么断开？怎样恢复？", 2),
    ("2023年生效的规范\n培训费用上限和审批人分别是谁？", 2),
    ("？？？", 1),
])
def test_conservative_anchors_preserve_question_parts(question, count):
    requirements = question_requirements(question)
    assert len(requirements) == count
    assert "".join(question.split()) == "".join("".join(item.text.split()) for item in requirements)
    assert json.loads(requirement_prompt(question))[0]['id'] == 'Q1'


def test_long_question_tail_is_not_silently_discarded():
    question = "\n".join(f"问题{index}？" for index in range(20))
    requirements = question_requirements(question)
    assert len(requirements) == 8 and '问题19？' in requirements[-1].text


def test_one_anchor_can_have_multiple_semantic_aspects_and_shared_claims():
    answer = draft([row(aspect="费用上限"), row(aspect="审批角色")])
    assert not coverage_errors(answer, "培训费用上限和审批人分别是谁？")


@pytest.mark.parametrize("coverage", [
    [row()],  # missing Q2
    [row(), row("Q9")],
    [row(), row("Q2", claim_indices=[99])],
    [row(), row("Q2", status="insufficient", claim_indices=[], missing_information="没有例外证据")],
    [row(), row("Q2", claim_indices=[])],
    [row(), row("Q2", claim_indices=[1, 1])],
    [row(), row("Q2"), row("Q2")],
    [row(), row("Q2", aspect="   ")],
    [row(), row("Q2", missing_information="实际缺证据")],
])
def test_incomplete_or_dishonest_structure_is_rejected(coverage):
    assert coverage_errors(draft(coverage), "谁审批？有什么例外？")


def test_refusal_keeps_missing_and_withheld_requirements_without_partial_claims():
    answer = draft([
        row(status="withheld", claim_indices=[], missing_information="整体拒答，未输出已有流程"),
        row("Q2", status="insufficient", claim_indices=[], missing_information="没有兼职适用规则"),
    ], answerable=False, claims=[])
    assert not coverage_errors(answer, "如何申请？兼职是否适用？")


def test_generation_requires_coverage_and_strict_positive_claim_indices():
    # Put the answer plan before claims in the structured schema to encourage
    # planning within the same call, not merely describing an already written answer.
    assert next(iter(GenerationAnswerDraft.model_json_schema()['properties'])) == 'coverage'
    with pytest.raises(ValidationError):
        GenerationAnswerDraft(answerable=True, claims=[])
    for index in (0, -1, True, "1"):
        with pytest.raises(ValidationError):
            draft([row(claim_indices=[index])])
