from types import SimpleNamespace

from backend.app.knowledge.answer_constraints import complete_cited_table_conditions, generation_constraints, temporal_evidence_context
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


def policy_context(**policy):
    return SimpleNamespace(items=[SimpleNamespace(citation_id='资料1', policy=policy)])


def test_historical_reminder_does_not_require_a_current_replacement_number():
    evidence = policy_context(effective_from='2024-01-01', effective_to='2025-06-30', business_status='已归档')
    relation = temporal_evidence_context('2024年住宿标准？', evidence.items)
    assert relation['evidence'][0]['relation'] == 'covers_query_period'
    text = generation_constraints('截至2026-10-04，2024年标准？', evidence)
    assert '资料1' in text and '不要求额外找到现行替代金额' in text
    assert '同一时期、同一对象和场景' in text
    assert '550' not in text  # No answer-specific facts embedded in the aid.


def test_partial_unknown_invalid_and_outside_period_are_not_certified_as_covering():
    for policy, expected in [
        ({'effective_from': '2024-07-01', 'effective_to': '2025-01-01'}, 'overlaps_query_period'),
        ({'effective_from': '2025-01-01', 'effective_to': '2025-12-31'}, 'outside_query_period'),
        ({'effective_from': '2024-01-01'}, 'unknown'),
        ({'effective_from': '2025-01-01', 'effective_to': '2023-01-01'}, 'metadata_warning'),
        ({'effective_from': 'bad', 'review_date': '2025-01-01'}, 'unknown'),
    ]:
        evidence = policy_context(**policy)
        assert temporal_evidence_context('2024年标准？', evidence.items)['evidence'][0]['relation'] == expected
        assert '有效期覆盖业务时间的资料：' not in generation_constraints('2024年标准？', evidence)
    assert temporal_evidence_context('2024年和2025年比较？', []) is None
