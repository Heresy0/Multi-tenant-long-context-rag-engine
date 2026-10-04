"""Applicability selection over already-authorized candidates, never grants access."""
import re
from datetime import date

from langchain_core.documents import Document

from ..documents.policy_metadata import DATE, extract_policy, parse_date, policy_label
from .evidence_quality import evidence_role, prefer_content


def query_period(question, today=None):
    dates = {parse_date(match.group(1)) for match in re.finditer(DATE, question)} - {None}
    if len(dates) == 1:
        value = next(iter(dates))
        return value, value
    if len(dates) > 1:
        return None  # Comparison/range intent is not guessed in v1.
    years = set(re.findall(r"(?<!\d)(\d{4})年", question))
    if len(years) == 1:
        year = int(next(iter(years)))
        if 1 <= year <= 9999:
            return date(year, 1, 1), date(year, 12, 31)
    if not years and re.search(r"当前|现在|现行|最新", question):
        value = today or date.today()
        return value, value
    return None


def select_evidence(question, documents):
    """Remove only explicitly inapplicable policies; preserve unknown/conflicts.

    Supersession requires explicit ``supersedes`` document codes plus matching
    ``rule_scope`` administrator metadata; neither date nor filename implies it.
    """
    period = query_period(question)
    selected = []
    by_source = {}
    for doc in documents:
        key = str(doc.metadata.get("document_id") or doc.metadata.get("source_id") or doc.metadata.get("source") or doc.metadata.get("document_name", ""))
        by_source.setdefault(key, []).append(doc)
    for group in by_source.values():
        inferred = extract_policy("\n".join(doc.page_content for doc in group), str(group[0].metadata.get("document_name", "")))
        for doc in group:
            stored = doc.metadata.get("policy")
            policy = {**inferred, **(stored if isinstance(stored, dict) else {})}
            start, end = parse_date(policy.get("effective_from")), parse_date(policy.get("effective_to"))
            if period and ((start and start > period[1]) or (end and end < period[0])):
                continue
            # Supersession removes a prior rule only at a known applicable time.
            reason = "explicit_period_match" if period and (start or end) else "unknown_or_no_time_filter"
            selected.append(Document(page_content=doc.page_content, id=getattr(doc, "id", None),
                                     metadata={**doc.metadata, "policy": policy, "policy_selection": reason,
                                               "evidence_role": evidence_role(doc)}))
    replacements = {}
    if period:
        for doc in selected:
            p = doc.metadata["policy"]
            start, end = parse_date(p.get("effective_from")), parse_date(p.get("effective_to"))
            if p.get("rule_scope") and p.get("document_code") and start and start <= period[0] and (end is None or end >= period[1]):
                for code in p.get("supersedes", []) if isinstance(p.get("supersedes"), list) else []:
                    if code != p["document_code"]:
                        replacements[(str(code), str(p["rule_scope"]))] = p.get("document_code")
    # Follow explicit chains, but preserve evidence when a chain reaches a cycle.
    def acyclic(key):
        visited = set()
        while key in replacements:
            if key in visited:
                return False
            visited.add(key)
            key = (str(replacements[key]), key[1])
        return True
    replacements = {key: value for key, value in replacements.items() if acyclic(key)}
    selected = [doc for doc in selected if (str(doc.metadata["policy"].get("document_code")),
                str(doc.metadata["policy"].get("rule_scope"))) not in replacements]
    # Prefer actual content before the final top-k; topic preference must not
    # promote a specialist cover over usable body evidence from other documents.
    def identity(doc):
        return (str(doc.metadata.get("document_id") or doc.metadata.get("source", "")),
                getattr(doc, "id", None) or doc.metadata.get("chunk_id") or doc.page_content)
    indices = {identity(doc): i for i, doc in enumerate(documents)}
    webhook_only = bool(re.search("Webhook", question, re.I)) and not re.search(r"API|开放平台", question, re.I)
    content_first = prefer_content(question)
    def priority(doc):
        p = doc.metadata["policy"]
        specific = webhook_only and "webhook" in p.get("topics", [])
        front_matter = content_first and doc.metadata["evidence_role"] == "front_matter"
        return (front_matter, not specific, indices.get(identity(doc), len(documents)))
    selected.sort(key=priority)
    return selected


def render_for_rerank(doc):
    label = policy_label(doc.metadata.get("policy", {}))
    return (label + "\n" if label else "") + doc.page_content
