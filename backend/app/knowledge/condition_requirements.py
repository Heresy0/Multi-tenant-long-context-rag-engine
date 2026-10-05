"""Bounded table prerequisites from authorized context, not a semantic planner.

Only explicit row labels in condition questions are expanded. Ambiguous rows,
truncated tables, and disjunctions are never guessed or combined across versions.
"""
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class ConditionRequirement:
    text: str
    citation_id: str


_HEADINGS = {'定义', '条件', '适用条件', '前提', '必要条件', '处理', '验收处理', '审批要求'}
_ASK = re.compile(r'条件|要求|要满足|需要满足|什么前提|哪些前提')


def _cells(line):
    return [cell.strip() for cell in line.strip().strip('|').split('|')]


def _clauses(text):
    # Do not transform alternatives/exceptions into mandatory conjunctions.
    if re.search(r'或|除非|否则|任一|至少一个', text):
        return [text]
    return [part.strip() for part in re.split(r'[，,；;]|并且|并|且', text) if part.strip()]


def condition_requirements(question, context):
    if context is None or not _ASK.search(question):
        return []
    segments = re.split(r'[？?；;\n]', question)
    candidates = {}
    for item in context.items:
        if getattr(item, 'evidence_role', 'content') != 'content':
            continue
        header = None
        for line in item.content.splitlines():
            cells = _cells(line)
            if len(cells) < 2:
                header = None
                continue
            if any(cell in _HEADINGS for cell in cells[1:]):
                header = cells
                continue
            if not header or len(cells) != len(header) or len(line) > 600:
                continue
            if all(re.fullmatch(r'[:\-\s]+', cell or ' ') for cell in cells):
                continue
            label = cells[0]
            if not 2 <= len(label) <= 32:
                continue
            code = re.match(r'[A-Za-z]+\d+', label)
            token = code.group() if code else label
            pattern = r'(?<![A-Za-z0-9])' + re.escape(token) + r'(?![A-Za-z0-9])'
            if not any(_ASK.search(segment) and re.search(pattern, segment, re.I) for segment in segments):
                continue
            fields = [(name, value) for name, value in zip(header[1:], cells[1:]) if name in _HEADINGS]
            # An empty recognized field may indicate clipped or incomplete evidence.
            if not fields or any(not value or '…' in value or '...' in value for _, value in fields):
                continue
            candidates.setdefault(token.casefold(), []).append((label, fields, item.citation_id))
    result = []
    for variants in candidates.values():
        signatures = {tuple((name, re.sub(r'\s+', '', value)) for name, value in fields)
                      for _, fields, _ in variants}
        if len(signatures) != 1:
            continue
        label, fields, citation = variants[0]
        for name, value in fields:
            for clause in _clauses(value):
                result.append(ConditionRequirement(f'{label}的{name}：{clause}', citation))
    # Avoid silently dropping a subset of necessary conditions when the limit is hit.
    return result if len(result) <= 16 else []
