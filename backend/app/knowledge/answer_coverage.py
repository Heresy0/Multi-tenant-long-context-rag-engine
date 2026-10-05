"""Low-cost question anchors and structural coverage, not a semantic judge.

Q anchors come only from the user question. E facets come from unambiguous,
authorized condition tables. No extra planning/retrieval/model call is made.
"""
import json
import re
from dataclasses import dataclass

from .answer_models import GenerationAnswerDraft
from .condition_requirements import condition_requirements


@dataclass(frozen=True)
class QuestionRequirement:
    id: str
    text: str
    citation_id: str | None = None


def question_requirements(question: str, context=None) -> list[QuestionRequirement]:
    # Conservative syntactic anchors, not domain-specific or semantic splitting.
    # Keep any long tail together rather than dropping parts of the user's query.
    parts = [part.strip() for part in re.findall(r"[^？?；;\n]+[？?；;]?", question) if part.strip()]
    if not parts and question.strip():
        parts = [question.strip()]
    if len(parts) > 8:
        parts = parts[:7] + ["\n".join(parts[7:])]
    anchors = [QuestionRequirement(f"Q{index}", text) for index, text in enumerate(parts, 1)]
    return anchors + [QuestionRequirement(f"E{index}", row.text, row.citation_id)
                      for index, row in enumerate(condition_requirements(question, context), 1)]


def requirement_prompt(question: str, context=None) -> str:
    return json.dumps([dict(id=row.id, question=row.text,
                           **({'source': row.citation_id} if row.citation_id else {}))
                       for row in question_requirements(question, context)],
                      ensure_ascii=False)


def coverage_errors(draft: GenerationAnswerDraft, question: str, context=None) -> list[str]:
    requirements = {row.id: row for row in question_requirements(question, context)}
    expected = set(requirements)
    actual = {row.requirement_id for row in draft.coverage}
    errors = []
    if expected - actual:
        errors.append("缺少必答问题的覆盖记录：" + "、".join(sorted(expected - actual)))
    if actual - expected:
        errors.append("包含不存在的必答问题编号：" + "、".join(sorted(actual - expected)))
    seen = set()
    for row in draft.coverage:
        key = (row.requirement_id, re.sub(r"\s+", "", row.aspect))
        if not key[1] or key in seen:
            errors.append("必答要点为空或重复：" + row.requirement_id)
        seen.add(key)
        indices = row.claim_indices
        if len(indices) != len(set(indices)) or any(index > len(draft.claims) for index in indices):
            errors.append("必答要点关联了重复或不存在的结论：" + row.requirement_id)
        if row.status == "answered":
            if not indices or not draft.answerable or row.missing_information is not None:
                errors.append("已回答要点缺少结论或与拒答状态矛盾：" + row.requirement_id)
            required = requirements.get(row.requirement_id)
            if required and required.citation_id and not any(
                    required.citation_id in draft.claims[index - 1].citations
                    for index in indices if 1 <= index <= len(draft.claims)):
                errors.append("条件要点缺少对应原文引用：" + row.requirement_id)
        elif indices or not (row.missing_information or "").strip():
            errors.append("未回答要点必须说明缺口且不能关联结论：" + row.requirement_id)
        if draft.answerable and row.status != "answered":
            errors.append("仍有未回答必答要点，不能标为完整可回答：" + row.requirement_id)
    return errors


def coverage_snapshot(draft: GenerationAnswerDraft, question: str, context=None) -> dict:
    errors = coverage_errors(draft, question, context)
    return dict(requirements=json.loads(requirement_prompt(question, context)),
                coverage=[row.model_dump() for row in draft.coverage], errors=errors,
                structurally_complete=draft.answerable and not errors,
                limitation="Syntactic anchors and bounded table facets with model-declared coverage; not semantic completeness or faithfulness.")
