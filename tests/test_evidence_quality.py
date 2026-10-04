from pathlib import Path

import pytest
from langchain_core.documents import Document

from backend.app.knowledge.context_builder import ContextBuilder
from backend.app.knowledge.evidence_quality import evidence_role, prefer_content
from backend.app.knowledge.evidence_selection import select_evidence
from backend.app.knowledge.rerank_retriever import RerankRetriever
from backend.app.knowledge.reranker import BaseReranker, RerankError, RerankResult
from langchain_core.retrievers import BaseRetriever


def chunk(text, section="", identifier="one", **metadata):
    return Document(id=identifier, page_content="文档：投递规范\n章节：" + (section or "文档说明") + "\n\n" + text,
                    metadata={"document_name": "投递规范", "source_id": "policy", "source": "policy.txt",
                              "document_id": "policy", "section_path": section, **metadata})


def header_first():
    return [chunk("投递规范", identifier="title"),
            chunk("示例科技有限公司", identifier="company"),
            chunk("文档编号：RULE-001；版本：1.0；生效日期：2026年10月1日；状态：现行。", identifier="metadata"),
            chunk("虚构测试资料，仅用于测试，不具有实际法律效力。", identifier="disclaimer"),
            chunk("先校验签名，再处理消息。", "1 接口边界", "boundary"),
            chunk("HTTP 429或5xx触发重试；其他4xx直接进入失败待核查。", "2 成功与重试", "rule"),
            chunk("去重记录保留7日。", "3 幂等", "idempotency"),
            chunk("日志不包含签名密钥。", "4 日志", "logs"),
            chunk("手工重放需要授权。", "5 故障边界", "manual")]


@pytest.mark.parametrize("index", range(4))
def test_known_preamble_is_front_matter(index):
    assert evidence_role(header_first()[index]) == "front_matter"


@pytest.mark.parametrize("text", [
    "其他4xx直接进入失败待核查。", "普通员工住宿限额650元。",
    "生效日期：2026年10月1日；必须先取得审批。", "审批由平台研发负责人完成。",
    "星海项目的业务处理结果待核实。",
])
def test_unheaded_or_mixed_business_prose_is_not_downgraded(text):
    assert evidence_role(chunk(text)) == "content"


def test_metadata_table_and_unknown_heading_are_conservatively_classified():
    assert evidence_role(chunk("文档编号|RULE-001|版本|1.0")) == "front_matter"
    assert evidence_role(chunk("价格|650元|适用城市|上海")) == "content"
    assert evidence_role(chunk("需要先审批", section="文档说明")) == "content"


def test_context_reserves_budget_for_rules_even_when_called_without_selector():
    built = ContextBuilder().build(header_first()[:8], question="返回401会重试吗？")
    assert len(built.items) == 4
    assert "其他4xx直接进入失败待核查" in built.text
    assert [item.chunk_id for item in built.items] == ["boundary", "rule", "idempotency", "logs"]
    assert built.total_characters <= 12000


def test_metadata_only_questions_and_metadata_only_documents_remain_answerable():
    docs = header_first()
    question = "这份规范的生效日期是什么？"
    assert not prefer_content(question)
    built = ContextBuilder().build(docs, question=question)
    assert "生效日期：2026年10月1日" in built.text
    assert len(ContextBuilder().build(docs[:4], question="介绍文档").items) == 4
    # Mentioning a version or code in a business question must not disable body preference.
    assert prefer_content("Webhook返回401怎么办，依据哪个版本？")
    assert prefer_content("文档编号RULE-001中的返回401规则是什么？")


class Static(BaseRetriever):
    documents: list[Document]
    def _get_relevant_documents(self, query, *, run_manager):
        return self.documents


class HeaderFirst(BaseReranker):
    fail: bool = False
    def rerank(self, *, query, documents, top_n):
        if self.fail:
            raise RerankError("offline fake")
        # Deliberately rank all four covers above the body, but preserve body
        # relevance order. Arbitrarily bad body rankings are outside this fix.
        order = sorted(range(len(documents)), key=lambda i: evidence_role(Document(
            page_content=documents[i].rsplit("\n\n", 1)[-1],
            metadata={"document_name": "投递规范"})) != "front_matter")
        return [RerankResult(i, 1 - rank * .01) for rank, i in enumerate(order[:top_n])]


@pytest.mark.parametrize("fail", [False, True])
def test_body_survives_final_top_k_and_context_on_success_or_fallback(fail):
    retriever = RerankRetriever(base_retriever=Static(documents=header_first()), reranker=HeaderFirst(fail=fail),
        top_n=8, candidate_processor=select_evidence, result_processor=select_evidence)
    results = retriever.invoke("Webhook返回401是否重试？")
    assert len(results) == 8
    assert results[0].metadata["evidence_role"] == "content"
    assert "其他4xx直接进入失败待核查" in ContextBuilder().build(results).text


def test_specialist_cover_never_displaces_generic_business_rule():
    cover = chunk("Webhook专门规范", document_name="Webhook专门规范")
    body = chunk("其他4xx直接进入失败待核查。", "重试规则", "generic", document_name="接口通用规则")
    assert select_evidence("Webhook返回401怎么办？", [cover, body])[0].id == body.id


def test_real_corpus_header_first_reproduction_keeps_retry_body():
    root = Path("output/pdf/xinghai-department-corpus-v3/documents")
    paths = list(root.rglob("30_*.txt"))
    if not paths:
        pytest.skip("Local synthetic corpus not present")
    from backend.app.documents.text_splitter import split_txt
    chunks = split_txt(paths[0])
    assert len(chunks) == 9
    assert [evidence_role(doc) for doc in chunks[:4]] == ["front_matter"] * 4
    selected = select_evidence("Webhook返回401状态码时，也会自动重试吗？", chunks)[:8]
    built = ContextBuilder().build(selected)
    assert "其他4xx直接进入失败待核查" in built.text
