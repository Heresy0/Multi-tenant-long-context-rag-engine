"""Evidence-bound completeness aids. No additional model call or invented facts."""
import re

from .answer_models import AnswerClaim
from .temporal import query_period


def generation_constraints(question):
    period = query_period(question)
    temporal = (f"业务时间区间为{period[0].isoformat()}至{period[1].isoformat()}。"
                "查询日期不覆盖业务时间；历史问题可用当时有效的归档版本。\n") if period else ""
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
