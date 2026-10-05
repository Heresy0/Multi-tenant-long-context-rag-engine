import json

from backend.app.evaluation.metrics import score_generation, summarize_generation
from backend.app.evaluation.reporting import quality_layers, write_report
from backend.app.evaluation.runner import score_trace


def fixture():
    case = dict(id='independent-q1', question='培训费用及审批人？', answerable=True,
                required_facts=['800元', '负责人'], expected_evidence=[])
    first = dict(answer='培训费用800元', answerable=True, citations=[])
    final = dict(first, answer='培训费用800元，由负责人审批')
    trace = dict(response=final, stages=dict(
        context=[], answer_generation=[dict(response=first, claims=[],
            coverage=dict(structurally_complete=True), completion_changed=True, validation_errors=[])],
        answer_completion=[dict(kind='test_supplement', status='appended')]))
    return case, trace


def test_final_pass_cannot_hide_first_pass_missing_fact():
    case, trace = fixture()
    record = score_trace(case, trace, include_answer=True)
    assert record['status'] == 'passed'
    generation = record['metrics']['generation']
    assert not generation['first_pass_fact_complete'] and generation['first_pass']['missing_facts'] == ['负责人']
    assert generation['structurally_complete']  # Distinct from semantic/fact completeness.
    metrics = summarize_generation([record])
    assert metrics['first_pass_fact_complete_rate'] == 0
    assert metrics['supplement_append_rate'] == metrics['completion_change_rate'] == 1


def test_old_traces_errors_and_unlabelled_cases_are_not_fake_zero_or_perfect_scores():
    case, trace = fixture()
    old = dict(trace, stages=dict(context=[]))
    assert not score_generation(case, old)['observed']
    records = [score_trace(case, old, include_answer=True), dict(status='error')]
    metrics = summarize_generation(records)
    assert metrics['observed_cases'] == 0 and metrics['unobserved_cases'] == 2
    assert metrics['first_pass_proxy_pass_rate'] is None and metrics['supplement_append_rate'] is None
    assert score_generation(dict(case, required_facts=[]), trace)['first_pass_fact_complete'] is None


def test_negative_refusal_is_scored_separately_from_positive_fact_completeness():
    case, trace = fixture()
    negative = dict(case, answerable=False, required_facts=[])
    trace['stages']['answer_generation'][0].update(response=dict(answer='资料不足', answerable=False, citations=[]),
                                                   completion_changed=False)
    trace['stages']['answer_completion'] = []
    record = dict(metrics=dict(generation=score_generation(negative, trace)))
    metrics = summarize_generation([record])
    assert metrics['first_pass_proxy_pass_rate'] == 1 and metrics['first_pass_fact_complete_rate'] is None
    assert metrics['fact_labelled_positive_cases'] == 0 and metrics['completion_change_rate'] == 0


def test_report_shows_first_pass_failures_even_when_final_passes(tmp_path):
    case, trace = fixture()
    record = score_trace(case, trace, include_answer=True)
    quality = dict(records=[record])
    layers = quality_layers(quality)
    assert layers['answer']['generation_metrics']['first_pass_fact_complete_rate'] == 0
    write_report(dict(mode='replay', layers=dict(answer=layers['answer'])), tmp_path)
    text = (tmp_path / 'report.md').read_text(encoding='utf-8')
    assert '首次生成' in text and 'independent-q1' in text and '负责人' in text
    result = json.loads((tmp_path / 'report.json').read_text(encoding='utf-8'))
    assert result['layers']['answer']['records'][0]['generation']['first_pass_response']['answer'] == '培训费用800元'


def test_validation_failure_is_not_counted_as_correct_first_model_refusal():
    case, trace = fixture()
    trace['stages']['answer_generation'][0].update(
        response=dict(answer='安全校验拒答', answerable=False, citations=[]),
        validation_errors=['不存在的引用'])
    metrics = score_generation(dict(case, answerable=False, required_facts=[]), trace)
    assert metrics['first_pass']['answerability_correct']
    assert not metrics['first_pass']['automatic_proxy_pass']
