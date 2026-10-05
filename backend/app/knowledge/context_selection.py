"""Question-aware reservations inside the existing per-source context budget.

This is a bounded ordering heuristic over supplied evidence, not a relevance,
authority or truth classifier. It never fetches documents or invents facts.
"""
import re

from .evidence_quality import prefer_content

_EVENTS = ("验收", "上线", "发布", "交付", "修复", "恢复", "关闭", "付款")
_FINAL_QUERY = re.compile(r"最后|最终|实际|是否(?:已经|已)|何时|什么时候|哪天|日期|时间")
_CAUSE_QUERY = re.compile(r"为何|为什么|原因|根因|因何")
_DATE = re.compile(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}|\d{1,2}月\d{1,2}日")
_PLANNED = re.compile(r"计划|原定|拟于|预计|安排|目标|调整至")


def source_key(document):
    metadata = document.metadata
    return str(metadata.get("source_id") or metadata.get("document_id")
               or metadata.get("source") or "未知来源")


def _body(document):
    # Splitter wrappers/titles must not masquerade as operative statements.
    return "\n".join(line for line in document.page_content.splitlines()
                     if not re.match(r"^(?:文档|章节|页码)[:：]", line.strip()))


def _facets(question):
    facets = []
    if _FINAL_QUERY.search(question):
        facets.extend("event:" + event for event in _EVENTS if event in question)
    if _CAUSE_QUERY.search(question):
        facets.append("cause")
    return facets


def _support(document, facet):
    body = _body(document)
    if facet == "cause":
        return int(bool(re.search(r"根因|因为|由于|导致|原因(?:是|为)|要求新增", body)))
    # Completing an acceptance account/environment is not project acceptance.
    event = re.escape(facet.split(":", 1)[1]) + r"(?!账号|账户|环境|材料|准备|条件|脚本|记录|申请)"
    statements = re.split(r"[。；;,，\n]", body)
    scores = []
    for statement in statements:
        if not re.search(event, statement):
            continue
        # Explicit non-completion is also evidence for an actual-status question.
        actual = bool(re.search(
            rf"(?:签署|完成|通过|已于|已经|尚未|未能|未完成).{{0,30}}{event}"
            rf"|{event}.{{0,20}}(?:完成|通过|结束|尚未|未完成)", statement,
        ))
        if actual and not _PLANNED.search(statement):
            scores.append(2 + int(bool(_DATE.search(statement))))
    return max(scores, default=0)


def select_context_chunks(documents, *, question, max_per_source):
    """Reserve a directly supportive witness, then fill by original ranking.

    A source's top hit is retained when the budget permits. Ordinary and explicit
    metadata questions retain the original order. Negative facts are not removed.
    """
    facets = _facets(question) if prefer_content(question) else []
    groups = {}
    for index, document in enumerate(documents):
        groups.setdefault(source_key(document), []).append(index)
    selected, reservations = set(), []
    for indices in groups.values():
        witnesses = []
        # Only intervene when the source cap would actually discard candidates.
        if len(indices) > max_per_source:
            for facet in facets:
                witness = max(indices, key=lambda index: (_support(documents[index], facet), -index))
                if _support(documents[witness], facet) and witness not in witnesses:
                    witnesses.append(witness)
                    reservations.append(dict(chunk_id=str(getattr(documents[witness], "id", None)
                                                         or documents[witness].metadata.get("chunk_id") or ""),
                                             facet=facet))
        required = ([indices[0]] if max_per_source > 1 else []) + witnesses
        chosen = list(dict.fromkeys(required + indices))[:max_per_source]
        selected.update(chosen)
    # Retain global rerank order among the chosen chunks; citations stay stable.
    return [doc for index, doc in enumerate(documents) if index in selected], reservations
