from types import SimpleNamespace

from backend.app.knowledge.answer_coverage import coverage_errors, question_requirements
from backend.app.knowledge.answer_models import AnswerClaim, GenerationAnswerDraft
from backend.app.knowledge.condition_requirements import condition_requirements


def context(*texts):
    return SimpleNamespace(items=[SimpleNamespace(content=text, citation_id=f'资料{i}', evidence_role='content')
                                  for i, text in enumerate(texts, 1)])


TABLE = '级别|定义|验收处理\nD2重大|关键功能异常且无替代方案|修复并通过回归后验收\nD3一般|有替代方案，不阻断流程|客户书面接受并明确修复日期后可验收'


def test_conditions_expand_each_conjunct_but_not_unasked_other_row():
    rows = condition_requirements('D2可以验收吗？D3要满足什么条件？', context(TABLE))
    assert [row.text for row in rows] == [
        'D3一般的定义：有替代方案', 'D3一般的定义：不阻断流程',
        'D3一般的验收处理：客户书面接受', 'D3一般的验收处理：明确修复日期后可验收']
    assert all(row.citation_id == '资料1' for row in rows)
    assert len(condition_requirements('D2和D3分别要满足什么要求？', context(TABLE))) == 8
    assert not condition_requirements('D30要满足什么条件？', context(TABLE))


def test_independent_labels_and_alternatives_keep_logical_scope():
    procurement = '类型|适用条件|审批要求\n紧急采购|生产中断或存在安全风险|主管签字并事后补录'
    rows = condition_requirements('紧急采购有什么条件和审批要求？', context(procurement))
    assert [row.text for row in rows] == ['紧急采购的适用条件：生产中断或存在安全风险',
                                        '紧急采购的审批要求：主管签字', '紧急采购的审批要求：事后补录']
    vpn = '等级|前提|处理\nL7高级|已批准且持有效设备证书|管理员登记并开通只读权限'
    assert len(condition_requirements('L7需要满足哪些条件？', context(vpn))) == 4


def test_conflicting_incomplete_metadata_and_overflow_rows_are_not_guessed():
    assert not condition_requirements('D3有什么条件？', context(TABLE, TABLE.replace('有替代方案', '无替代方案')))
    assert not condition_requirements('D3有什么条件？', context('级别|定义|验收处理\nD3一般|有替代方案|'))
    assert not condition_requirements('D3有什么条件？', context(TABLE.replace('客户书面接受', '...')))
    evidence = context(TABLE)
    evidence.items[0].evidence_role = 'metadata'
    assert not condition_requirements('D3有什么条件？', evidence)
    large = '等级|条件|处理\n' + '\n'.join(f'L{i}|条件甲且条件乙|登记并审批' for i in range(5))
    assert not condition_requirements('L0 L1 L2 L3 L4需要什么条件？', context(large))
    assert len(condition_requirements('D3有什么条件？', context(TABLE, TABLE))) == 4


def test_each_derived_requirement_is_mandatory_and_binds_actual_citation():
    question, evidence = 'D3有什么条件？', context(TABLE)
    requirements = question_requirements(question, evidence)
    assert [row.id for row in requirements] == ['Q1', 'E1', 'E2', 'E3', 'E4']
    rows = [dict(requirement_id=row.id, aspect=row.text, status='answered', claim_indices=[1], missing_information=None) for row in requirements]
    draft = GenerationAnswerDraft(answerable=True, claims=[AnswerClaim(text='已有答案', citations=['资料1'])], coverage=rows)
    assert not coverage_errors(draft, question, evidence)
    draft.coverage.pop()
    assert any('E4' in error for error in coverage_errors(draft, question, evidence))
    draft.claims[0].citations = ['资料2']
    assert any('对应原文引用' in error for error in coverage_errors(draft, question, evidence))
