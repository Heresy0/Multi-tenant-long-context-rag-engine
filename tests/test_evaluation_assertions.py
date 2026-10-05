import json
from pathlib import Path

import pytest

from backend.app.evaluation.metrics import score_answer


ROOT = Path(__file__).resolve().parents[1]


def cases():
    single = [json.loads(line) for line in (ROOT / 'evals/xinghai_v3/single_turn.jsonl').read_text(encoding='utf-8').splitlines()]
    sessions = json.loads((ROOT / 'evals/xinghai_v3/conversations.json').read_text(encoding='utf-8'))
    extra = [json.loads(line) for line in (ROOT / 'evals/first_pass_generalization/single_turn.jsonl').read_text(encoding='utf-8').splitlines()]
    return {row['id']: row for row in single + extra + [dict(turn, answerable=True) for session in sessions for turn in session['turns']]}


def response(case, text):
    return dict(answerable=True, answer=text, citations=[dict(document_name=name)
                for name in case.get('required_source_documents', []) or [item['document'] for item in case.get('expected_evidence', [])]])


@pytest.mark.parametrize('identifier,fact,text', [
    ('XH-E-052', '不自动删除', '不等于自动删除'),
    ('XH-MT-08-T3', '不会自动删除', '不会把数据库记录一并删掉，不自动删除数据库原记录'),
    ('FP-008', '不因delivery_attempt变化再次执行业务', '即使delivery_attempt变化，也不应重新执行业务'),
])
def test_reviewed_equivalent_negatives_pass_without_removing_other_requirements(identifier, fact, text):
    case = cases()[identifier]
    case = dict(case, required_facts=[fact])
    assert score_answer(case, response(case, text))['automatic_proxy_pass']
    assert not score_answer(case, dict(response(case, text), citations=[]))['automatic_proxy_pass']
    assert not score_answer(dict(case, required_facts=[fact, '123元']), response(case, text))['automatic_proxy_pass']


@pytest.mark.parametrize('text', [
    '会自动删除数据库记录', '并非不自动删除', '不能说不会自动删除',
    '不自动删除，但会自动删除数据库记录', '不自动删除但是会把数据库记录一并删掉',
])
def test_negation_reversals_and_explicit_conflicts_are_not_synonym_passes(text):
    case = dict(cases()['XH-E-052'], required_facts=['不自动删除'])
    assert not score_answer(case, response(case, text))['automatic_proxy_pass']


@pytest.mark.parametrize('text', [
    '不应重新执行业务。但需要重新执行业务', '不是不应重新执行业务',
    '不应重新执行业务，但会再次执行业务', '应重新执行业务',
])
def test_delivery_reversal_and_conflict_are_not_accepted(text):
    case = dict(cases()['FP-008'], required_facts=['不因delivery_attempt变化再次执行业务'])
    assert not score_answer(case, response(case, text))['automatic_proxy_pass']


def test_negation_rules_are_opt_in_and_cannot_replace_citation_evidence():
    case = dict(cases()['XH-E-052'], required_facts=['不自动删除'])
    result = score_answer(case, response(case, '不自动删除，会自动删除数据库记录'))
    assert result['contradictory_facts']
    plain = dict(case, fact_assertions={}, fact_alternatives={})
    assert not score_answer(plain, response(plain, '不等于自动删除'))['automatic_proxy_pass']
    r = response(case, '不自动删除')
    r['citations'] = [dict(document_name='错误来源', citation_id='资料99')]
    assert not score_answer(case, r, [])['automatic_proxy_pass']
