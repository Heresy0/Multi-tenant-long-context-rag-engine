import pytest
from pydantic import ValidationError

from backend.app.answer_models import (
    AnswerClaim,
    AnswerDraft,
    AnswerResult,
    Citation,
)


def test_answer_draft_uses_independent_claim_lists() -> None:
    first = AnswerDraft(answerable=False)
    second = AnswerDraft(answerable=False)

    first.claims.append(
        AnswerClaim(text="测试结论")
    )

    assert second.claims == []


def test_answer_claim_rejects_empty_text() -> None:
    with pytest.raises(ValidationError):
        AnswerClaim(text="")


def test_answer_result_serializes_nested_citation() -> None:
    result = AnswerResult(
        answer="迟到应按照考勤制度处理。[资料1]",
        answerable=True,
        citations=[
            Citation(
                citation_id="资料1",
                document_name="员工制度",
                section_path="考勤 > 迟到",
                source="C:/docs/policy.docx",
                chunk_id="chunk-1",
            )
        ],
    )

    dumped = result.model_dump()

    assert dumped["answerable"] is True
    assert dumped["citations"][0]["citation_id"] == "资料1"
    assert dumped["citations"][0]["chunk_id"] == "chunk-1"
    assert dumped["refusal_reason"] is None


def test_refusal_result_can_omit_citations() -> None:
    result = AnswerResult(
        answer="根据当前知识库资料，暂时无法回答这个问题。",
        answerable=False,
        citations=[],
        refusal_reason="没有检索到相关资料。",
    )

    assert result.citations == []
    assert result.refusal_reason == "没有检索到相关资料。"
