"""Bounded document-local expansion for explicit incident identifiers."""
import re
from uuid import UUID

from langchain_core.documents import Document

_INCIDENT = re.compile(r"(?<![A-Za-z0-9_-])INC-\d{4}-\d{3,8}(?![A-Za-z0-9_-])", re.I)


def expand_incident_candidates(question, candidates, *, repository, scope, budget=30):
    identifiers = {value.upper() for value in _INCIDENT.findall(question)}
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
