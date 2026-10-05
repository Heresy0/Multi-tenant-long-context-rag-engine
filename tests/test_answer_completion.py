from types import SimpleNamespace

from backend.app.knowledge.answer_completion import complete_answer
from backend.app.knowledge.answer_constraints import generation_constraints
from backend.app.knowledge.answer_models import AnswerClaim, AnswerDraft


def item(content, citation='资料1', doc='doc', section='', incidents=()):
    return SimpleNamespace(content=content, citation_id=citation, document_id=doc,
                           section_path=section, matched_incident_ids=incidents, policy={})


def context(*items):
    return SimpleNamespace(items=list(items))


def draft(text='向量候选最多20条，重排最多保留5条', citation='资料1'):
    return AnswerDraft(answerable=True, claims=[AnswerClaim(text=text, citations=[citation])])


TABLE = ('阶段|目标配置|注意事项\n向量候选|最多20条|范围过滤\n'
         '关键词候选|PostgreSQL BM25最多20条|参数绑定\n'
         '融合|RRF取20条|跨授权库共享\n重排|最多保留5条|失败降级')
BUDGET_QUESTION = '跨库检索的候选和重排预算是多少？'
CAUSE_QUESTION = 'INC-2028-0123是Webhook的重试问题吗？实际根因是什么？'
CAUSE = '客户端收到429后新建请求并生成新的幂等键，因此被识别为不同任务。'


def test_budget_completion_copies_actual_rows_not_fixed_numbers_and_is_idempotent():
    answer = draft()
    decision = complete_answer(BUDGET_QUESTION, answer, context(item(TABLE)))
    assert decision[0]['status'] == 'appended'
    assert 'RRF取20条' in answer.claims[-1].text and 'BM25' in answer.claims[-1].text
    assert answer.claims[-1].citations == ['资料1']
    assert '30条' not in answer.claims[-1].text
    complete_answer(BUDGET_QUESTION, answer, context(item(TABLE)))
    assert len(answer.claims) == 2


def test_uncited_incomplete_or_conflicting_budget_tables_are_not_synthesized():
    for evidence in (context(item(TABLE, '资料2')),
                     context(item(TABLE.replace('融合|RRF取20条|跨授权库共享\n', ''))),
                     context(item(TABLE), item(TABLE.replace('RRF取20条', 'RRF取12条'), '资料2'))):
        answer = draft()
        complete_answer(BUDGET_QUESTION, answer, evidence)
        assert len(answer.claims) == 1
    answer = draft()
    complete_answer('重排最多几条？', answer, context(item(TABLE)))
    assert len(answer.claims) == 1


def test_obvious_stage_number_contradiction_refuses_instead_of_appending():
    answer = draft('重排最多保留20条')
    decision = complete_answer(BUDGET_QUESTION, answer, context(item(TABLE)))
    assert not answer.answerable and not answer.claims
    assert decision[0]['status'] == 'refused_conflicting_value'


def roots(cause=CAUSE, incidents=('INC-2028-0123',), doc='incident'):
    return context(item('事件INC-2028-0123发生了重复提交。', doc='incident'),
                   item(cause, '资料2', doc, '3 根因与影响', incidents))


def test_root_referral_is_completed_only_from_linked_cited_incident_and_real_chunk():
    answer = draft('不是Webhook，实际根因应参照事件复盘。')
    evidence = roots()
    decision = complete_answer(CAUSE_QUESTION, answer, evidence)
    assert decision[0]['status'] == 'appended'
    assert answer.claims[-1].text.endswith(CAUSE)
    assert answer.claims[-1].citations == ['资料2']
    assert '资料2' in generation_constraints(CAUSE_QUESTION, evidence)
    complete_answer(CAUSE_QUESTION, answer, evidence)
    assert len(answer.claims) == 2


def test_cause_requires_explicit_link_real_paragraph_and_previously_cited_document():
    for evidence in (roots(incidents=()), roots(incidents=('INC-2028-9999',)),
                     roots(doc='other'), roots(cause='实际根因应参照技术部复盘。'),
                     roots(cause='章节：根因是客户端错误，因此失败。'),
                     roots(cause='根因：' + '长' * 601)):
        answer = draft('根因参照复盘。')
        complete_answer(CAUSE_QUESTION, answer, evidence)
        assert len(answer.claims) == 1


def test_multiple_incidents_or_ambiguous_causes_are_not_merged():
    for question, evidence in (
        (CAUSE_QUESTION + '与INC-2028-9999对比？', roots()),
        (CAUSE_QUESTION, context(*roots().items, item('根因是服务端异常导致失败。',
                                 '资料3', 'incident', '根因', ('INC-2028-0123',)))),
    ):
        answer = draft('根因参照复盘。')
        complete_answer(question, answer, evidence)
        assert len(answer.claims) == 1


def test_refused_draft_remains_refused_with_budget_or_root_evidence():
    for question, evidence in ((BUDGET_QUESTION, context(item(TABLE))), (CAUSE_QUESTION, roots())):
        answer = AnswerDraft(answerable=False, refusal_reason='有真实冲突。')
        assert not complete_answer(question, answer, evidence)
        assert not answer.answerable and not answer.claims


def full_budget(text=None):
    return draft(text or '向量候选最多20条，关键词（PostgreSQL BM25）候选最多20条。\n'
                 '融合阶段通过RRF取20条，且跨授权库共享候选预算，并非每个库各20条。\n重排阶段最多保留5条。')


def test_verified_stage_paraphrase_is_not_repeated_or_extended_with_unasked_weights():
    answer = full_budget()
    decision = complete_answer(BUDGET_QUESTION, answer, context(item(TABLE)))
    assert len(answer.claims) == 1 and decision[0]['status'] == 'already_present_verified_stages'


def test_missing_stage_or_shared_scope_still_gets_evidence_supplement():
    for text in ('向量候选最多20条，关键词BM25最多20条，重排最多保留5条。',
                 full_budget().claims[0].text.replace('跨授权库共享候选预算，并非每个库各20条', '统一预算')):
        answer = draft(text)
        decision = complete_answer(BUDGET_QUESTION, answer, context(item(TABLE)))
        assert len(answer.claims) == 2 and decision[0]['status'] == 'appended'


def test_stage_swapped_values_and_affirmative_per_library_budget_refuse():
    for text in ('向量候选最多5条，关键词BM25最多20条，RRF取20条，重排最多保留20条。',
                 full_budget().claims[0].text.replace('并非每个库各20条', '每个库各20条')):
        answer = draft(text)
        decision = complete_answer(BUDGET_QUESTION, answer, context(item(TABLE)))
        assert not answer.answerable and decision[0]['status'] == 'refused_conflicting_value'


def test_non_shared_source_does_not_inherit_shared_scope_rules():
    for note in ('各库独立配置', '各库独立配置，不共享'):
        answer = full_budget(full_budget().claims[0].text.replace('跨授权库共享候选预算，并非每个库各20条', '每个库各20条'))
        evidence = context(item(TABLE.replace('跨授权库共享', note)))
        decision = complete_answer(BUDGET_QUESTION, answer, evidence)
        assert answer.answerable and decision[0]['status'] != 'refused_conflicting_value'


def test_actual_cited_ordered_cause_is_not_repeated_and_metrics_are_not_added():
    source = '客户端在HTTP 429后立即创建新请求，并在每次重试时生成新的幂等键，服务端因此将其视为不同任务。已确认的首次响应耗时为13分钟，复核结束耗时为68分钟。'
    answer = draft('实际根因是客户端在收到HTTP 429响应后立即创建新请求，并在每次重试时生成新的幂等键，导致服务端将其视为不同的任务而非重复请求。', '资料2')
    decision = complete_answer(CAUSE_QUESTION, answer, roots(cause=source))
    assert len(answer.claims) == 1 and decision[0]['status'] == 'already_present_verified_mechanism'
    incomplete = draft('根因参照事件复盘。')
    complete_answer(CAUSE_QUESTION, incomplete, roots(cause=source))
    assert '创建新请求' in incomplete.claims[-1].text and '耗时' not in incomplete.claims[-1].text
    timed = draft('根因参照事件复盘。')
    complete_answer(CAUSE_QUESTION + '响应耗时呢？', timed, roots(cause=source))
    assert '13分钟' in timed.claims[-1].text


def test_unknown_negated_partial_and_only_overview_cited_causes_keep_fallback():
    for text, citation in (
        ('客户端收到429后新建请求，因此被识别为不同任务。', '资料2'),
        ('客户端收到429后没有新建请求并生成新的幂等键，因此被识别为不同任务。', '资料2'),
        (CAUSE, '资料1'),
        ('根因可能是客户端收到429后新建请求并生成新的幂等键，因此被识别为不同任务。', '资料2'),
    ):
        answer = draft(text, citation)
        decision = complete_answer(CAUSE_QUESTION, answer, roots())
        assert len(answer.claims) == 2 and decision[0]['status'] == 'appended'
