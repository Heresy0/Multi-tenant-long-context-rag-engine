from datetime import date
from types import SimpleNamespace

import pytest

from backend.app.knowledge.temporal import clean_followup_time, future_certainty_gap, query_period


@pytest.mark.parametrize('question,expected', [
    ('截至2026-10-04，2025年上海标准？', (date(2025, 1, 1), date(2025, 12, 31))),
    ('截至2026年10月4日，2025年上海标准？', (date(2025, 1, 1), date(2025, 12, 31))),
    ('查询日期为2026/10/04，2025年2月标准？', (date(2025, 2, 1), date(2025, 2, 28))),
    ('2024年2月标准？', (date(2024, 2, 1), date(2024, 2, 29))),
    ('2025年与2026年标准比较', None),
    ('2026-06-30与2026-07-01标准比较', None),
    ('2025年13月标准？', None),
    ('截至2026-10-04，当前标准？', (date(2026, 10, 4), date(2026, 10, 4))),
])
def test_business_period(question, expected):
    assert query_period(question) == expected


def test_followup_cleanup_never_drops_user_reference_or_changed_business_time():
    rewritten = '截至2026-10-04，普通员工2025年上海标准？'
    assert clean_followup_time('2025年呢？', rewritten) == '普通员工2025年上海标准？'
    assert clean_followup_time('截至2026-10-04问2025年', rewritten) == rewritten
    assert clean_followup_time('2024年呢？', rewritten) == rewritten
    assert clean_followup_time('负责人呢？', rewritten) == rewritten


def test_resolver_applies_cleanup_after_model_rewrite_without_an_extra_call():
    from backend.app.knowledge.conversation_service import ConversationResolver, ResolvedQuestion
    resolver = object.__new__(ConversationResolver)
    calls = []
    def invoke(messages):
        calls.append(messages)
        return ResolvedQuestion(question='截至2026-10-04，普通员工2025年去上海的住宿限额及版本？')
    resolver.model = SimpleNamespace(invoke=invoke)
    turns = [SimpleNamespace(question='当前标准？', retrieval_question='截至2026-10-04，当前普通员工上海标准？',
                             result_json={'answerable': True, 'answer': '650元'})]
    result = resolver.resolve('2025年呢？', turns)
    assert result == '普通员工2025年去上海的住宿限额及版本？'
    assert len(calls) == 1


def item(**policy):
    return SimpleNamespace(policy=policy)


def test_future_certainty_requires_covering_or_future_effective_rule_not_review_date():
    q, today = '2027年10月已经确定的上海限额？', date(2026, 10, 5)
    assert future_certainty_gap(q, [item(effective_from='2026-07-01', review_date='2027-06-30')], today)
    assert not future_certainty_gap(q, [item(effective_from='2026-07-01', effective_to='2027-12-31')], today)
    assert not future_certainty_gap(q, [item(effective_from='2027-01-01')], today)
    assert future_certainty_gap(q, [item(effective_from='2027-01-01', business_status='草案')], today)
    assert future_certainty_gap(q, [item(effective_from='2026-07-01', effective_to='2027-10-10')], today)
    assert not future_certainty_gap('当前规则将来是否可能变化？', [], today)
    assert not future_certainty_gap('2025年已确定的标准？', [], today)
