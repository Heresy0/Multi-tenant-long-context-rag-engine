"""Bounded document-local expansion for explicit incident identifiers."""
import re
from uuid import UUID

from langchain_core.documents import Document

_INCIDENT = re.compile(r"(?<![A-Za-z0-9_-])INC-\d{4}-\d{3,8}(?![A-Za-z0-9_-])", re.I)


def incident_identifiers(question):
    return {value.upper() for value in _INCIDENT.findall(question)}


def asks_incident_cause(question):
    return bool(incident_identifiers(question) and re.search(r'根因|原因|为何|为什么', question))


def incident_cause_quote(content, section):
    """A bounded operative paragraph, not a title or referral to another source."""
    if not re.search(r'根因|原因(?:分析)?', section):
        return None
    for line in content.splitlines():
        line = line.strip()
        if (not line or re.match(r'^(?:文档|章节|页码)[:：]', line) or '|' in line
                or len(line) > 600 or re.search(r'应参照|应参考|请查阅|请参考|参见|详见', line)):
            continue
        if re.search(r'因此|导致|由于|因为|造成|引发|问题来自|根因(?:是|为|[:：])', line):
            return line
    return None


def incident_source_matches(question, documents):
    """Bind companions to an explicit incident in the same document/version."""
    requested = incident_identifiers(question)
    matches = {}
    for doc in documents:
        key = str(doc.metadata.get('document_id') or doc.metadata.get('source_id')
                  or doc.metadata.get('source') or '')
        found = requested.intersection(incident_identifiers(doc.page_content))
        if key and found:
            matches.setdefault(key, set()).update(found)
    return matches


def expand_incident_candidates(question, candidates, *, repository, scope, budget=30):
    identifiers = incident_identifiers(question)
    if not identifiers:
        return candidates
    anchors = []
    for doc in candidates:
        if not scope.contains(doc.metadata.get('tenant_id'), doc.metadata.get('knowledge_base_id')):
            continue
        if not identifiers.intersection(value.upper() for value in _INCIDENT.findall(doc.page_content)):
            continue
        try:
            identifier = UUID(str(doc.metadata.get('document_id')))
        except ValueError:
            continue
        if identifier not in anchors:
            anchors.append(identifier)
    anchors = anchors[:2]
    if not anchors:
        return candidates
    companions = repository.candidate_companions(scope=scope, document_ids=anchors)
    expanded = [Document(page_content=chunk.content, metadata=chunk.metadata, id=chunk.chunk_id)
                for chunk in companions if scope.contains(chunk.tenant_id, chunk.knowledge_base_id)]
    # Keep matched-document candidates first, then companions, then other hits.
    # Reranking still sees at most the original shared budget.
    anchor_ids = {str(identifier) for identifier in anchors}
    ordered = ([doc for doc in candidates if str(doc.metadata.get('document_id')) in anchor_ids]
               + expanded + [doc for doc in candidates if str(doc.metadata.get('document_id')) not in anchor_ids])
    result, seen = [], set()
    for doc in ordered:
        if not scope.contains(doc.metadata.get('tenant_id'), doc.metadata.get('knowledge_base_id')):
            continue
        key = (str(doc.metadata.get('document_id')), doc.metadata.get('chunk_id') or doc.page_content)
        if key not in seen:
            seen.add(key)
            result.append(doc)
        if len(result) >= budget:
            break
    return result
