from types import SimpleNamespace

from backend.app.knowledge.answer_constraints import complete_cited_table_conditions, generation_constraints
from backend.app.knowledge.answer_models import AnswerClaim, AnswerDraft

TABLE = '''级别|定义|验收处理
D1阻断|越权、数据错误或核心流程不能完成|不得进入正式验收
D3一般|存在可接受替代方案，不阻断核心流程|客户书面接受并明确修复日期后可验收'''


def draft(text='D3客户书面接受后可验收', citations=None):
    return AnswerDraft(answerable=True, claims=[AnswerClaim(text=text, citations=citations or ['资料1'])])


def context(text=TABLE, citation='资料1'):
    return SimpleNamespace(items=[SimpleNamespace(content=text, citation_id=citation)])


def test_completes_all_conditions_from_cited_row_only_and_is_idempotent():
    answer = draft()
    complete_cited_table_conditions('D3可以验收吗，需要哪些条件？', answer, context())
    assert len(answer.claims) == 2
    assert '不阻断核心流程' in answer.claims[-1].text
    assert '明确修复日期' in answer.claims[-1].text
    assert answer.claims[-1].citations == ['资料1']
    complete_cited_table_conditions('D3可以验收吗，需要哪些条件？', answer, context())
    assert len(answer.claims) == 2


def test_uncited_unrelated_refused_or_conflicting_rows_are_not_synthesized():
    for question, answer, evidence in [
        ('D3验收条件？', draft(citations=['资料2']), context()),
        ('D3是什么？', draft(), context()),
        ('D4验收条件？', draft(), context()),
        ('D3验收条件？', draft('D1不得验收'), context()),
        ('D3验收条件？', draft(), context(TABLE + '\nD3一般|有替代方案|不得验收')),
    ]:
        complete_cited_table_conditions(question, answer, evidence)
        assert len(answer.claims) == 1
    answer = AnswerDraft(answerable=False, claims=[], refusal_reason='资料不足')
    complete_cited_table_conditions('D3验收条件？', answer, context())
    assert not answer.claims


def test_not_limited_to_specific_company_or_d3_rule():
    answer = draft('A2可以进入', ['资料1'])
    complete_cited_table_conditions('A2允许进入的条件？', answer, context('级别|定义|处理\nA2|须持有效许可证|经过安全检查方可进入'))
    assert '有效许可证' in answer.claims[-1].text


def test_obvious_condition_bypass_is_refused_not_silently_appended():
    answer = draft('D3无需客户书面接受就可直接验收')
    complete_cited_table_conditions('D3验收条件？', answer, context())
    assert not answer.answerable and not answer.claims
    assert '矛盾' in answer.refusal_reason


def test_generation_time_hint_uses_business_year():
    assert '2025-01-01至2025-12-31' in generation_constraints('截至2026-10-04，2025年标准？')
