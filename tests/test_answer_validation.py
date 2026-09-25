import pytest

from backend.app.answer_models import (
    AnswerClaim,
    AnswerDraft,
)
from backend.app.answer_validation import AnswerValidator
from backend.app.context_builder import (
    BuiltContext,
    ContextItem,
)


def make_context(*contents: str) -> BuiltContext:
    items = [
        ContextItem(
            citation_id=f"资料{index}",
            content=content,
            source=f"C:/docs/document-{index}.docx",
            document_name=f"文档{index}",
            section_path="测试章节",
            chunk_id=f"chunk-{index}",
            rerank_score=0.9,
        )
        for index, content in enumerate(
            contents,
            start=1,
        )
    ]
    text = "\n\n".join(contents)

    return BuiltContext(
        text=text,
        items=items,
        total_characters=len(text),
    )


def make_answerable_draft(
    text: str,
    citations: list[str] | None = None,
) -> AnswerDraft:
    return AnswerDraft(
        answerable=True,
        claims=[
            AnswerClaim(
                text=text,
                citations=(
                    citations
                    if citations is not None
                    else ["资料1"]
                ),
            )
        ],
    )


def validate(
    draft: AnswerDraft,
    context: BuiltContext,
    question: str = "",
):
    return AnswerValidator().validate(
        draft=draft,
        context=context,
        question=question,
    )


def test_valid_claim_with_existing_citation_passes() -> None:
    result = validate(
        make_answerable_draft("员工应遵守考勤制度"),
        make_context("员工应遵守考勤制度。"),
    )

    assert result.valid is True
    assert result.errors == []


def test_answerable_draft_requires_at_least_one_claim() -> None:
    result = validate(
        AnswerDraft(answerable=True),
        make_context("知识库内容"),
    )

    assert result.valid is False
    assert any(
        "至少需要一个结论" in error
        for error in result.errors
    )


def test_claim_requires_citation() -> None:
    result = validate(
        make_answerable_draft("测试结论", citations=[]),
        make_context("测试结论"),
    )

    assert result.valid is False
    assert any(
        "没有引用" in error
        for error in result.errors
    )


def test_unknown_citation_is_rejected() -> None:
    result = validate(
        make_answerable_draft(
            "测试结论",
            citations=["资料99"],
        ),
        make_context("测试结论"),
    )

    assert result.valid is False
    assert any(
        "不存在的引用" in error
        for error in result.errors
    )


def test_refusal_without_claims_passes() -> None:
    result = validate(
        AnswerDraft(
            answerable=False,
            refusal_reason="知识库资料不足",
        ),
        make_context("无关资料"),
    )

    assert result.valid is True
    assert result.errors == []


def test_refusal_with_claims_is_rejected() -> None:
    result = validate(
        AnswerDraft(
            answerable=False,
            claims=[
                AnswerClaim(
                    text="确定性结论",
                    citations=["资料1"],
                )
            ],
        ),
        make_context("确定性结论"),
    )

    assert result.valid is False
    assert any(
        "拒答结果不应同时包含" in error
        for error in result.errors
    )


def test_exact_number_does_not_match_larger_number() -> None:
    result = validate(
        make_answerable_draft("补贴为10元"),
        make_context("补贴为100元"),
    )

    assert result.valid is False


@pytest.mark.parametrize(
    ("claim", "evidence"),
    [
        ("补贴为1000元", "补贴为1,000元"),
        ("补贴为10.0元", "补贴为10元"),
        ("余额为-10元", "余额为-10.00元"),
    ],
)
def test_equivalent_number_formats_match(
    claim: str,
    evidence: str,
) -> None:
    result = validate(
        make_answerable_draft(claim),
        make_context(evidence),
    )

    assert result.valid is True


def test_percentage_does_not_match_plain_number() -> None:
    result = validate(
        make_answerable_draft("抵扣比例为10%"),
        make_context("抵扣金额为10元"),
    )

    assert result.valid is False


@pytest.mark.parametrize(
    ("claim", "evidence"),
    [
        ("可用性为99.20%", "月度可用性为99.2%"),
        ("可用性为９９．２％", "月度可用性为99.2%"),
    ],
)
def test_equivalent_percentage_formats_match(
    claim: str,
    evidence: str,
) -> None:
    result = validate(
        make_answerable_draft(claim),
        make_context(evidence),
    )

    assert result.valid is True


def test_different_dates_do_not_match() -> None:
    result = validate(
        make_answerable_draft(
            "制度于2026年7月2日生效"
        ),
        make_context("制度于2026年7月1日生效"),
    )

    assert result.valid is False


@pytest.mark.parametrize(
    ("claim", "evidence"),
    [
        (
            "制度于2026年07月01日生效",
            "制度生效日期为2026-7-1",
        ),
        (
            "制度于2026/7/1生效",
            "制度生效日期为2026年7月1日",
        ),
    ],
)
def test_equivalent_date_formats_match(
    claim: str,
    evidence: str,
) -> None:
    result = validate(
        make_answerable_draft(claim),
        make_context(evidence),
    )

    assert result.valid is True


def test_multiple_citations_are_combined_as_evidence() -> None:
    result = validate(
        make_answerable_draft(
            "补贴为100元，可用性为99.2%",
            citations=["资料1", "资料2"],
        ),
        make_context(
            "补贴标准为100元。",
            "月度可用性为99.2%。",
        ),
    )

    assert result.valid is True


def test_invalid_date_is_checked_without_crashing() -> None:
    result = validate(
        make_answerable_draft(
            "制度于2026年2月30日生效"
        ),
        make_context("制度于2026年2月28日生效"),
    )

    assert result.valid is False
    assert result.errors


def test_number_from_question_can_be_used_with_range_evidence() -> None:
    result = validate(
        make_answerable_draft(
            "月度可用性为99.2%时，服务费抵扣比例为10%"
        ),
        make_context(
            "月度可用性低于99.5%但不低于99.0%时，"
            "服务费抵扣比例为10%。"
        ),
        question=(
            "月度可用性为99.2%时"
            "服务抵扣比例是多少？"
        ),
    )

    assert result.valid is True
    assert result.errors == []


def test_number_absent_from_question_and_evidence_is_rejected() -> None:
    result = validate(
        make_answerable_draft(
            "月度可用性为99.2%时，服务费抵扣比例为20%"
        ),
        make_context(
            "月度可用性低于99.5%但不低于99.0%时，"
            "服务费抵扣比例为10%。"
        ),
        question=(
            "月度可用性为99.2%时"
            "服务抵扣比例是多少？"
        ),
    )

    assert result.valid is False
    assert any(
        "20%" in error
        for error in result.errors
    )
