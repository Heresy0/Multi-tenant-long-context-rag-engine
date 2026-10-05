"""Evidence-bound completeness aids. No additional model call or invented facts."""
import re

from .answer_models import AnswerClaim
from .temporal import query_period
from ..documents.policy_metadata import parse_date
from .answer_completion import incident_cause_items


def temporal_evidence_context(question, items):
    """Expose time relations, not a verdict about factual sufficiency/conflicts."""
    period = query_period(question)
    if not period:
        return None
    relations = []
    for item in items:
        policy = getattr(item, 'policy', None)
        policy = policy if isinstance(policy, dict) else {}
        start, end = parse_date(policy.get('effective_from')), parse_date(policy.get('effective_to'))
        relation = 'unknown'
        if policy.get('metadata_warning') or (start and end and start > end):
            relation = 'metadata_warning'
        elif start and end:
            relation = ('outside_query_period' if start > period[1] or end < period[0]
                        else 'covers_query_period' if start <= period[0] and end >= period[1]
                        else 'overlaps_query_period')
        elif (start and start > period[1]) or (end and end < period[0]):
            relation = 'outside_query_period'
        relations.append(dict(citation_id=item.citation_id, relation=relation,
                              effective_from=start.isoformat() if start else None,
                              effective_to=end.isoformat() if end else None,
                              archived=policy.get('business_status') in ('已归档', '历史归档', '归档')))
    return dict(query_from=period[0].isoformat(), query_to=period[1].isoformat(), evidence=relations)


def generation_constraints(question, context=None):
    period = query_period(question)
    temporal = (f"业务时间区间为{period[0].isoformat()}至{period[1].isoformat()}。"
                "查询日期不覆盖业务时间；历史问题可用当时有效的归档版本。\n") if period else ""
    applicability = temporal_evidence_context(question, context.items) if context else None
    if applicability:
        covering = [row['citation_id'] for row in applicability['evidence']
                    if row['relation'] == 'covers_query_period']
        overlapping = [row['citation_id'] for row in applicability['evidence']
                       if row['relation'] == 'overlaps_query_period']
        if covering:
            temporal += '原文明示有效期覆盖业务时间的资料：' + '、'.join(covering) + '。\n'
        if overlapping:
            temporal += '只与业务时间部分重叠的资料：' + '、'.join(overlapping) + '；须按生效阶段回答，不能代表整个期间。\n'
        temporal += (
            '时间判定：归档状态和“当前不适用/当前标准不同”的提醒只限制当前使用，'
            '不自动否定原文明示有效期内的历史规则。历史规则已有直接证据时，'
            '不要求额外找到现行替代金额才能回答历史金额。'
            '仅因不同时期标准不同不能判资料冲突；只有同一时期、同一对象和场景的规则相互矛盾，'
            '且无法依据明确适用关系消解时，才说明冲突。'
            '时间覆盖标签不证明角色、场景或事实充分；未知有效期不能猜测，其他真实冲突仍保留。\n'
        )
    roots = incident_cause_items(question, context) if context else []
    if roots:
        temporal += ('已通过同文档事件编号关联、直接包含根因原文的资料：'
                     + '、'.join(item.citation_id for item, _ in roots)
                     + '。请直接解释原文原因和导致结果的机制；“参照复盘/详见某资料”不是根因答案。\n')
    return temporal + (
        "完整性检查：回答允许/验收条件时保留定义和处理列的全部必要条件；"
        "回答次数时区分总次数、追加重试次数及是否含首次；比较候选预算时分别说明每路、融合、重排及共享范围。"
        "复审日期不是失效日期；现行规则不等于已经承诺未来规则。"
        "资料注明虚拟测试不妨碍按该资料回答测试问题，但不能声称为真实公司事实。"
    )


def complete_cited_table_conditions(question, draft, context):
    """Copy an unambiguous definition/handling row from already cited evidence.

    Only explicit coded-rule condition questions are eligible. Conflicting rows,
    uncited evidence and calculation claims are deliberately left untouched.
    This is not a semantic correctness validator.
    """
    if not draft.answerable or not re.search(r"条件|验收|允许|是否可以", question):
        return
    codes = set(re.findall(r"(?<![A-Za-z0-9])([A-Z]\d{1,3})(?!\d)", question))
    if not codes:
        return
    citations = {citation for claim in draft.claims if claim.calculation is None
                 for citation in claim.citations}
    rows = {}
    for item in context.items:
        if item.citation_id not in citations:
            continue
        columns = None
        for line in item.content.splitlines():
            cells = [cell.strip() for cell in line.strip().strip('|').split('|')]
            if len(cells) < 3:
                columns = None
                continue
            if '定义' in cells and any('处理' in cell for cell in cells):
                columns = (cells.index('定义'), next(i for i, cell in enumerate(cells) if '处理' in cell))
                continue
            if columns is None or max(columns) >= len(cells):
                continue
            match = re.match(r"^([A-Z]\d{1,3})(?!\d)", cells[0])
            if match and match[1] in codes and all(cells[index] for index in columns):
                row = (cells[0], cells[columns[0]], cells[columns[1]])
                rows.setdefault(match[1], {}).setdefault(row, item.citation_id)
    for code in sorted(codes)[:3]:
        choices = rows.get(code, {})
        if len(choices) != 1:
            continue
        (label, definition, handling), citation = next(iter(choices.items()))
        relevant = [claim for claim in draft.claims if claim.calculation is None
                    and citation in claim.citations and re.search(rf"(?<![A-Za-z0-9]){re.escape(code)}(?!\d)", claim.text)]
        if not relevant:
            continue
        text = ''.join(claim.text for claim in relevant)
        if re.search(r'后可|方可|须|必须|只有', handling) and re.search(r'无需|无须|不需要|无条件|直接(?:验收|进入)', text):
            draft.answerable = False
            draft.claims = []
            draft.refusal_reason = '生成结果与已引用的必要条件存在明显矛盾。'
            return
        if definition not in text or handling not in text:
            draft.claims.append(AnswerClaim(
                text=f"关于{label}，表格定义：{definition}；处理规则：{handling}", citations=[citation],
            ))
