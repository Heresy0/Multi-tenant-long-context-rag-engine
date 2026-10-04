from datetime import date
from pathlib import Path

import pytest
from langchain_core.documents import Document

from backend.app.documents.policy_metadata import enrich_chunks, extract_policy
from backend.app.knowledge.evidence_selection import query_period, select_evidence
from backend.app.knowledge.context_builder import ContextBuilder
from backend.app.knowledge.rerank_retriever import RerankRetriever
from backend.app.knowledge.reranker import BaseReranker, RerankResult, RerankError
from langchain_core.retrievers import BaseRetriever


def document(name, content, policy=None, identifier=None):
    return Document(id=identifier or name, page_content=content,
                    metadata={"document_name": name, "source": name, "document_id": name,
                              "policy": policy or {}})


def versions():
    return [document("现行差旅制度", "普通员工650元", {"effective_from": "2026-07-01", "business_version": "1.4"}),
            document("历史差旅制度", "普通员工550元", {"effective_from": "2025-01-01", "effective_to": "2026-06-30", "business_version": "1.2", "business_status": "归档"})]


def test_labelled_dates_status_and_review_not_expiry():
    p = extract_policy("文档编号|POL-2026-01|版本|1.4\n生效日期|2026年7月1日|复审日期|2027年6月30日\n适用范围|全体员工", "差旅制度")
    assert p["effective_from"] == "2026-07-01" and p["business_version"] == "1.4"
    assert p["document_code"] == "POL-2026-01" and p["applicability"] == "全体员工"
    assert "effective_to" not in p
    p = extract_policy("状态:已归档。有效期为2025年1月1日至2026年6月30日。", "住宿标准")
    assert p["effective_from"] == "2025-01-01" and p["effective_to"] == "2026-06-30"
    assert p["business_status"] == "已归档"


@pytest.mark.parametrize("question,expected", [("2025年上海住宿限额", "历史差旅制度"),
    ("截至2026-10-04当前上海住宿限额", "现行差旅制度"),
    ("2026年6月30日住宿限额", "历史差旅制度"), ("2026年7月1日住宿限额", "现行差旅制度")])
def test_historical_and_current_boundaries(question, expected):
    assert [d.metadata["document_name"] for d in select_evidence(question, versions())] == [expected]


def test_year_with_policy_change_keeps_both_and_unknown_never_guessed():
    assert len(select_evidence("2026年限额是多少", versions())) == 2
    assert len(select_evidence("2025年和2026年标准比较", versions())) == 2
    assert query_period("当前标准", today=date(2026, 10, 4)) == (date(2026, 10, 4),) * 2
    unknown = document("未知", "没有元数据")
    assert unknown.page_content in [d.page_content for d in select_evidence("2020年标准", [*versions(), unknown])]
    p = extract_policy("生效日期:2026年2月30日;复审日期:2027年1月1日", "流程")
    assert "effective_from" not in p and "effective_to" not in p


def test_supersession_requires_same_rule_scope_and_full_period():
    old = document("旧规则", "旧条款", {"document_code": "RULE-A", "rule_scope": "Webhook重试", "effective_from": "2025-01-01"})
    new = document("新规则", "新条款", {"document_code": "RULE-B", "rule_scope": "Webhook重试", "supersedes": ["RULE-A"], "effective_from": "2026-07-01"})
    assert len(select_evidence("2025年规则", [old, new])) == 1
    assert len(select_evidence("2026年规则", [old, new])) == 2  # Mid-year transition is not silently erased.
    assert [d.id for d in select_evidence("截至2026-10-04规则", [old, new])] == [new.id]
    new.metadata["policy"]["rule_scope"] = "API重试"
    assert len(select_evidence("截至2026-10-04规则", [old, new])) == 2


def test_topic_preference_does_not_erase_cross_topic_or_unknown_evidence():
    generic = document("API通用手册", "API和Webhook通用说明")
    dedicated = document("Webhook专门规范", "其他4xx失败待核查")
    assert select_evidence("Webhook返回401怎么办", [generic, dedicated])[0].id == dedicated.id
    assert len(select_evidence("Webhook返回401怎么办", [generic, dedicated])) == 2
    assert select_evidence("开放API与Webhook比较", [generic, dedicated])[0].id == generic.id
    assert select_evidence("API和Webhook有什么区别", [generic, dedicated])[0].id == generic.id


def test_metadata_on_every_chunk_and_context():
    chunks = [Document(page_content="生效日期:2026年7月1日;版本:1.4"), Document(page_content="住宿650元")]
    enrich_chunks(chunks, "差旅制度")
    assert chunks[1].metadata["policy"]["effective_from"] == "2026-07-01"
    built = ContextBuilder().build(chunks)
    assert "生效日期：2026-07-01" in built.text
    assert built.items[0].policy["business_version"] == "1.4"


def test_self_supersession_and_cycles_preserve_conflicting_evidence():
    a = document("规则甲", "条款甲", {"document_code": "A", "rule_scope": "同一规则",
                 "supersedes": ["A", "B"], "effective_from": "2025-01-01"})
    b = document("规则乙", "条款乙", {"document_code": "B", "rule_scope": "同一规则",
                 "supersedes": ["A"], "effective_from": "2025-01-01"})
    assert len(select_evidence("2026-10-04规则", [a, b])) == 2
    assert len(select_evidence("2026-10-04规则", [a])) == 1


def test_explicit_acyclic_supersession_chain_keeps_final_version():
    docs = [document(code, code, {"document_code": code, "rule_scope": "同一规则",
            "supersedes": prior, "effective_from": "2025-01-01"})
            for code, prior in [("A", []), ("B", ["A"]), ("C", ["B"])]]
    assert [doc.id for doc in select_evidence("2026-10-04规则", docs)] == ["C"]


def test_policy_header_is_citable_for_version_and_date_not_new_business_amounts():
    from backend.app.knowledge.answer_models import AnswerClaim, AnswerDraft
    from backend.app.knowledge.answer_validation import AnswerValidator
    ctx = ContextBuilder().build([document("差旅制度", "住宿650元", {
        "effective_from": "2026-07-01", "business_version": "1.4"})])
    draft = AnswerDraft(answerable=True, claims=[AnswerClaim(
        text="版本1.4于2026年7月1日生效，住宿650元。", citations=["资料1"])])
    assert AnswerValidator().validate(draft=draft, context=ctx).valid
    draft.claims[0].text = "住宿999元。"
    assert not AnswerValidator().validate(draft=draft, context=ctx).valid


def test_malformed_optional_policy_does_not_break_selection():
    doc = document("无元数据", "条款")
    doc.metadata["policy"] = None
    assert len(select_evidence("2025年", [doc])) == 1


def test_identical_clauses_keep_distinct_business_version_citations():
    docs = versions()
    for doc in docs:
        doc.page_content = "同一条共同规定"
    ctx = ContextBuilder().build(select_evidence("2026年制度", docs))
    assert len(ctx.items) == 2
    assert {item.policy["business_version"] for item in ctx.items} == {"1.2", "1.4"}


class Static(BaseRetriever):
    documents: list[Document]
    def _get_relevant_documents(self, query, *, run_manager):
        return self.documents


class Reverse(BaseReranker):
    fail: bool = False
    def rerank(self, *, query, documents, top_n):
        if self.fail:
            raise RerankError("offline fake")
        return [RerankResult(i, .9) for i in reversed(range(len(documents)))][:top_n]


@pytest.mark.parametrize("fail", [False, True])
def test_policy_precedes_final_top_k_even_on_reranker_failure(fail):
    candidates = [document("Webhook规范", "明确规则"), document("通用API", "概述")]
    retriever = RerankRetriever(base_retriever=Static(documents=candidates), reranker=Reverse(fail=fail),
                               top_n=1, candidate_processor=select_evidence, result_processor=select_evidence)
    results = retriever.invoke("Webhook重试")
    assert len(results) == 1 and results[0].id == candidates[0].id


def test_real_corpus_metadata_and_historical_selection():
    root = Path("output/pdf/xinghai-department-corpus-v3/documents")
    if not root.exists():
        pytest.skip("Local synthetic corpus not present")
    from backend.app.documents.document_splitter import split_docx
    from backend.app.documents.pdf_splitter import split_pdf
    current = next(root.rglob("02_*.docx"))
    old = next(root.rglob("08_*.pdf"))
    current_chunks = enrich_chunks(split_docx(current), current.stem)
    old_chunks = enrich_chunks(split_pdf(old), old.stem)
    for d in current_chunks:
        d.metadata.update(document_id="current", document_name=current.name)
    for d in old_chunks:
        d.metadata.update(document_id="old", document_name=old.name)
    selected = select_evidence("2025年普通员工在上海住宿限额多少", [*current_chunks, *old_chunks])
    assert selected and all(d.metadata["document_id"] == "old" for d in selected)
    assert any("550元" in d.page_content for d in selected)
