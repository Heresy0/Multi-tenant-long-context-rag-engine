"""Deterministic evidence/answer metrics. These are proxies, not LLM judges."""
import re
import unicodedata
from pathlib import PurePosixPath
from urllib.parse import unquote


def normalize(value):
    return "".join(unicodedata.normalize("NFKC", str(value)).split()).replace("|", "")


def document_key(value):
    name = PurePosixPath(unquote(str(value)).replace("\\", "/")).name
    return re.sub(r"\.(docx|pdf|md|txt)$", "", name, flags=re.I)


def contains_fact(text, fact):
    """Literal proxy with numeric boundaries: 2次 must not match 12次."""
    needle = normalize(fact)
    if not needle:
        return False
    prefix = r"(?<![\d.])" if needle[0].isdigit() else ""
    suffix = r"(?![\d.])" if needle[-1].isdigit() else ""
    return re.search(prefix + re.escape(needle) + suffix, normalize(text)) is not None


def evidence_metrics(evidence, rows, *, alternative_sets=()):
    if alternative_sets:
        sets = [evidence, *[group for group in alternative_sets if group]]
        results = [evidence_metrics(group, rows) for group in sets if group]
        if results:
            result = max(results, key=lambda value: (value['all_evidence_present'], value['evidence_recall'], value['reciprocal_rank']))
            return dict(result, matched_evidence_set=results.index(result))
    if not evidence:
        return dict(scored=False, reason="no_positive_evidence_labels")
    matched, first_rank = set(), None
    for rank, row in enumerate(rows, 1):
        for index, item in enumerate(evidence):
            if (document_key(row.get("document_name", "")) == document_key(item["document"])
                    and contains_fact(row.get("content", ""), item["text"])):
                matched.add(index)
                if first_rank is None:
                    first_rank = rank
    return dict(scored=True, hit=bool(matched), first_relevant_rank=first_rank,
                reciprocal_rank=1 / first_rank if first_rank else 0,
                evidence_recall=len(matched) / len(evidence),
                all_evidence_present=len(matched) == len(evidence),
                expected_evidence_count=len(evidence), matched_evidence_count=len(matched))


def score_answer(case, response, context=None):
    if not isinstance(response, dict) or type(response.get("answerable")) is not bool:
        raise ValueError("Response requires boolean answerable")
    if not isinstance(response.get("answer"), str) or not isinstance(response.get("citations"), list):
        raise ValueError("Response requires answer and citations")
    facts = case.get("required_facts", [])
    alternatives = case.get("fact_alternatives", {})
    missing = [fact for fact in facts if not any(contains_fact(response["answer"], option)
               for option in [fact, *alternatives.get(fact, [])])]
    expected = {document_key(item["document"]) for item in case.get("expected_evidence", [])}
    expected.update(document_key(name) for name in case.get("required_source_documents", []))
    cited = {document_key(item.get("document_name", "")) for item in response["citations"]}
    source_sets = [expected, *[{document_key(name) for name in group}
                              for group in case.get("accepted_source_document_sets", []) if group]]
    # Complete, explicitly annotated alternatives only. Never union partial sets.
    expected = max(source_sets, key=lambda group: (group.issubset(cited), len(group & cited) / len(group) if group else 0))
    supported = None
    integrity = None
    if context is not None:
        matched_context = []
        integrity = True
        for citation in response["citations"]:
            candidates = [row for row in context if row.get("citation_id") == citation.get("citation_id")
                          and document_key(row.get("document_name", "")) == document_key(citation.get("document_name", ""))
                          and (not citation.get("chunk_id") or row.get("chunk_id") == citation["chunk_id"])
                          and (not citation.get("knowledge_base_id") or row.get("knowledge_base_id") == str(citation["knowledge_base_id"]))]
            if not candidates:
                integrity = False
            matched_context.extend(candidates)
        if case.get("expected_evidence"):
            supported = evidence_metrics(case["expected_evidence"], matched_context,
                                         alternative_sets=case.get("accepted_evidence_sets", []))["evidence_recall"]
    answerability_correct = response["answerable"] == case["answerable"]
    if case["answerable"]:
        proxy_pass = (answerability_correct and not missing and expected.issubset(cited)
                      and integrity is not False and (supported is None or supported == 1))
    else:
        proxy_pass = answerability_correct and not response["citations"]
    return dict(answerability_correct=answerability_correct, missing_facts=missing,
                fact_coverage=(len(facts) - len(missing)) / len(facts) if facts else None,
                all_required_sources_cited=expected.issubset(cited) if expected else None,
                citation_source_recall=len(expected & cited) / len(expected) if expected else None,
                citation_integrity=integrity, cited_evidence_recall=supported,
                automatic_proxy_pass=proxy_pass,
                semantic_correctness="requires_manual_review",
                faithfulness="requires_manual_review",
                known_issue=case.get("known_issue"))


def summarize_records(records):
    counts = {status: sum(row["status"] == status for row in records)
              for status in ("passed", "failed", "error", "skipped", "partial", "not_applicable")}
    return dict(total=len(records), **counts,
                pass_rate=counts["passed"] / (counts["passed"] + counts["failed"])
                if counts["passed"] + counts["failed"] else None)


def summarize_stages(records):
    result = {}
    for stage in ("vector", "keyword", "fusion", "rerank", "context"):
        scored = [row["metrics"][stage] for row in records if row.get("metrics", {}).get(stage, {}).get("scored")]
        result[stage] = dict(scored_cases=len(scored),
                            hit_rate=sum(row["hit"] for row in scored) / len(scored) if scored else None,
                            mrr=sum(row["reciprocal_rank"] for row in scored) / len(scored) if scored else None,
                            mean_evidence_recall=sum(row["evidence_recall"] for row in scored) / len(scored) if scored else None,
                            all_evidence_rate=sum(row["all_evidence_present"] for row in scored) / len(scored) if scored else None)
    return result
