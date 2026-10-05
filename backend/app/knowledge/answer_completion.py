"""Bounded, explicitly quoted supplements from this turn's authorized evidence.

Not a semantic judge: refusals stay refusals, ambiguous tables/causes stay
untouched, and normal citation/numeric validation still runs afterwards.
"""
import re

from .answer_models import AnswerClaim
from .evidence_expansion import asks_incident_cause, incident_identifiers, incident_cause_quote


_STAGES = {
    'vector': ('向量候选', '向量检索'),
    'keyword': ('关键词候选', 'BM25候选', 'BM25'),
    'fusion': ('融合', 'RRF'),
    'rerank': ('重排',),
}
_CAPACITY = re.compile(r'(?:最多(?:保留)?|取|上限(?:为)?|保留)\s*(\d+)\s*条')
_STAGE_MENTIONS = re.compile('|'.join(sorted(
    (re.escape(label) for labels in _STAGES.values() for label in labels), key=len, reverse=True)))


def _normalize(text):
    return re.sub(r'\s+', '', text)


def _budget_coverage(draft, citation, rows):
    """Match capacities to their own stage, never to a bag of answer numbers."""
    observed = {key: set() for key in _STAGES}
    texts = [claim.text for claim in draft.claims
             if claim.calculation is None and citation in claim.citations]
    text = _normalize('\n'.join(texts))
    for sentence in re.split(r'[。；\n]', '\n'.join(texts)):
        groups = []
        for match in _STAGE_MENTIONS.finditer(sentence):
            stage = next(key for key, labels in _STAGES.items() if match.group() in labels)
            if not groups or groups[-1][0] != stage:
                groups.append((stage, match.start()))
        for index, (stage, start) in enumerate(groups):
            end = groups[index + 1][1] if index + 1 < len(groups) else len(sentence)
            region = sentence[start:end]
            # Only affirmative, explicit capacities are eligible; alternatives stay unknown.
            capacity = _CAPACITY.search(region)
            prefix = sentence[max(0, start - 4):start]
            if not capacity or re.search(r'不|未|可能', prefix):
                continue
            if re.search(r'不是|并非|不应|不能|不得|不超过|可能|或者|或为', region[:capacity.end()]):
                continue
            values = set(_CAPACITY.findall(region))
            if len(values) == 1:
                observed[stage].update(int(value) for value in values)
    expected = {key: int(_CAPACITY.search(row.split('|')[1]).group(1)) for key, row in rows.items()}
    if any(values - {expected[key]} for key, values in observed.items()):
        return 'conflict'
    shared = '共享' in rows['fusion'] and not re.search(r'不共享|并非共享|不是共享', rows['fusion'])
    if shared:
        for match in re.finditer(r'不共享|每(?:个)?(?:授权)?(?:知识)?库(?:各|独立)', text):
            if not re.search(r'并非$|不是$|而非$', text[max(0, match.start() - 3):match.start()]):
                return 'conflict'
    if any(observed[key] != {expected[key]} for key in _STAGES):
        return 'unknown'
    if ('BM25' in rows['keyword'] and 'BM25' not in text
            or 'RRF' in rows['fusion'] and 'RRF' not in text):
        return 'unknown'
    if shared and not re.search(r'跨[^。；\n]{0,12}共享', text):
        return 'unknown'
    if '共享' in rows['fusion'] and not shared:
        return 'unknown'  # Negative sharing rules are not interpreted as affirmative sharing.
    return 'complete'


def _cause_only_quote(question, quote):
    if re.search(r'耗时|时长|时间|多久|响应|影响|截止', question):
        return quote
    # Drop only a trailing, explicitly labelled metrics sentence, not causal facts.
    parts = re.split(r'(?<=。)', quote)
    cutoff = next((index for index, part in enumerate(parts)
                   if index and re.match(r'\s*(?:已确认的)?(?:首次响应|响应|停止重复提交|复核结束).*耗时', part)), None)
    return ''.join(parts[:cutoff]) if cutoff is not None else quote


def _cause_is_covered(quote, draft, citation):
    """Conservative ordered lexical mechanism check; unfamiliar wording falls back."""
    clauses = [part.strip('。 ') for part in re.split(r'[，,；;]|并在|并且|并', quote) if part.strip('。 ')]
    anchors = []
    actions = effects = 0
    for clause in clauses:
        match = re.search(r'(?:创建|生成|新建|复用|写入|删除|发送|提交|视为|识别为|判定为).+', clause)
        if not match:
            continue
        anchor = match.group()
        if (re.search(r'不|未|没有|并非|不是|可能', clause[:match.start()])
                or re.search(r'或|可能|不是|并非|没有|不(?:应|会|能|得|再)|未(?:创建|生成|新建|复用)', anchor)):
            return False
        if anchor.startswith(('视为', '识别为', '判定为')):
            effects += 1
        else:
            actions += 1
        anchors.append(_normalize(anchor).replace('的', ''))
    if actions < 2 or effects < 1:
        return False
    for claim in draft.claims:
        if claim.calculation is not None or citation not in claim.citations:
            continue
        text = _normalize(claim.text).replace('的', '')
        if not re.search(r'导致|因此|所以|根因|原因', text):
            continue
        cursor = 0
        for anchor in anchors:
            index = text.find(anchor, cursor)
            if index < 0:
                break
            prefix = re.split(r'[。；，,]', text[max(0, index - 16):index])[-1]
            if re.search(r'不|未|没有|并非|不是|可能|或许|参照', prefix):
                break
            cursor = index + len(anchor)
        else:
            return True
    return False


def _stage_tables(item):
    tables, header, rows = [], None, {}

    def flush():
        if header and set(rows) == set(_STAGES):
            tables.append((item, header, rows.copy()))
        rows.clear()

    for line in item.content.splitlines():
        cells = [cell.strip() for cell in line.strip().strip('|').split('|')]
        if len(cells) < 2:
            flush()
            header = None
            continue
        if cells[0] == '阶段' and cells[1] in ('目标配置', '预算', '配置'):
            flush()
            header = '|'.join(cells)
            continue
        if header:
            stage = next((key for key, labels in _STAGES.items() if cells[0] in labels), None)
            if stage:
                # Duplicate stages or very long/unknown budget rows are ambiguous.
                if stage in rows or len(line) > 400 or not _CAPACITY.search(cells[1]):
                    flush()
                    header = None
                else:
                    rows[stage] = '|'.join(cells)
    flush()
    return tables


def complete_cited_stage_budgets(question, draft, context):
    if (not draft.answerable or not re.search('候选|检索', question)
            or '重排' not in question or not re.search('预算|多少|数量|配置', question)):
        return None
    tables = [table for item in context.items for table in _stage_tables(item)]
    citations = {citation for claim in draft.claims if claim.calculation is None
                 for citation in claim.citations}
    cited = [table for table in tables if table[0].citation_id in citations]
    if not cited:
        return dict(kind='stage_budgets', status='skipped_no_cited_complete_table')
    signatures = {tuple(_normalize(rows[key]) for key in _STAGES) for _, _, rows in tables}
    if len(signatures) != 1:
        return dict(kind='stage_budgets', status='skipped_conflicting_tables')
    item, header, rows = cited[0]
    coverage = _budget_coverage(draft, item.citation_id, rows)
    if coverage == 'conflict':
        draft.answerable, draft.claims = False, []
        draft.refusal_reason = '生成的阶段预算或跨库口径与已引用表格存在明确矛盾。'
        return dict(kind='stage_budgets', status='refused_conflicting_value')
    if coverage == 'complete':
        return dict(kind='stage_budgets', status='already_present_verified_stages')
    for claim in draft.claims:
        if claim.calculation is not None or item.citation_id not in claim.citations:
            continue
        for sentence in re.split(r'[。；\n]', claim.text):
            mentioned = [key for key, labels in _STAGES.items()
                         if any(label in sentence for label in labels)]
            values = _CAPACITY.findall(sentence)
            if len(mentioned) == 1 and len(values) == 1:
                expected = _CAPACITY.search(rows[mentioned[0]].split('|')[1]).group(1)
                if int(values[0]) != int(expected):
                    draft.answerable, draft.claims = False, []
                    draft.refusal_reason = '生成的阶段预算与已引用表格对应阶段存在明确矛盾。'
                    return dict(kind='stage_budgets', status='refused_conflicting_value')
    quote = header + '\n' + '\n'.join(rows[key] for key in _STAGES)
    text = '\n'.join(claim.text for claim in draft.claims
                     if claim.calculation is None and item.citation_id in claim.citations)
    if all(_normalize(row) in _normalize(text) for row in rows.values()):
        return dict(kind='stage_budgets', status='already_present')
    draft.claims.append(AnswerClaim(text='原文检索阶段配置：\n' + quote,
                                    citations=[item.citation_id]))
    return dict(kind='stage_budgets', status='appended', citation_id=item.citation_id)


def incident_cause_items(question, context):
    identifiers = incident_identifiers(question)
    if not asks_incident_cause(question) or len(identifiers) != 1:
        return []
    return [(item, quote) for item in context.items
            if identifiers.intersection(getattr(item, 'matched_incident_ids', ()))
            and (quote := incident_cause_quote(item.content, item.section_path))]


def complete_cited_incident_cause(question, draft, context):
    if not draft.answerable or not asks_incident_cause(question):
        return None
    roots = incident_cause_items(question, context)
    if not roots:
        return dict(kind='incident_cause', status='skipped_no_linked_cause')
    if len({_normalize(quote) for _, quote in roots}) != 1:
        return dict(kind='incident_cause', status='skipped_ambiguous_causes')
    cited_ids = {citation for claim in draft.claims if claim.calculation is None
                 for citation in claim.citations}
    cited_documents = {item.document_id for item in context.items
                       if item.citation_id in cited_ids and item.document_id}
    root = next(((item, quote) for item, quote in roots
                 if item.document_id and item.document_id in cited_documents), None)
    if root is None:
        return dict(kind='incident_cause', status='skipped_uncited_incident_document')
    item, quote = root
    quote = _cause_only_quote(question, quote)
    if _cause_is_covered(quote, draft, item.citation_id):
        return dict(kind='incident_cause', status='already_present_verified_mechanism')
    if any(item.citation_id in claim.citations and claim.calculation is None
           and _normalize(claim.text) in {_normalize(quote), _normalize('事件复盘原文的根因说明：' + quote)}
           for claim in draft.claims):
        return dict(kind='incident_cause', status='already_present')
    # Clearly label the source paragraph, and cite its actual chunk, not an overview.
    draft.claims.append(AnswerClaim(text='事件复盘原文的根因说明：' + quote,
                                    citations=[item.citation_id]))
    return dict(kind='incident_cause', status='appended', citation_id=item.citation_id)


def complete_answer(question, draft, context):
    return [decision for operation in (complete_cited_stage_budgets, complete_cited_incident_cause)
            if (decision := operation(question, draft, context)) is not None]
