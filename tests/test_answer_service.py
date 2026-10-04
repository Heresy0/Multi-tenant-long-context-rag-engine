from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid4, uuid5

from langchain_core.documents import Document
from langchain_core.messages import (
    HumanMessage,
    SystemMessage,
)
import pytest

from backend.app.knowledge import answer_service as service_module
from backend.app.knowledge.answer_models import (
    AnswerClaim,
    AnswerDraft,
)
from backend.app.knowledge.answer_service import (
    REFUSAL_TEXT,
    AnswerService,
)
from backend.app.knowledge.prompts import ANSWER_SYSTEM_PROMPT
from backend.app.security.retrieval_scope import (
    RetrievalScope,
)


TEST_SCOPE = RetrievalScope(
    tenant_id=uuid4(),
    knowledge_base_id=uuid4(),
)
TEST_SESSION = object()


class FakeRetrievalService:
    def __init__(
        self,
        documents: list[Document],
    ) -> None:
        self.documents = documents
        self.calls: list[dict] = []

    def search(
        self,
        query: str,
        *,
        scope: RetrievalScope,
        session: object,
    ) -> list[Document]:
        self.calls.append({
            "query": query,
            "scope": scope,
            "session": session,
        })
        return self.documents


class FakeStructuredLLM:
    def __init__(self, result) -> None:
        self.result = result
        self.calls: list[list] = []

    def invoke(self, messages: list):
        self.calls.append(messages)
        return self.result


class FakeChatModel:
    def __init__(
        self,
        structured_llm: FakeStructuredLLM,
    ) -> None:
        self.structured_llm = structured_llm
        self.schemas: list[type] = []

    def with_structured_output(self, schema: type):
        self.schemas.append(schema)
        return self.structured_llm


def make_document(
    content: str,
    *,
    name: str = "服务等级协议",
    section: str = "服务抵扣",
    chunk_id: str = "chunk-1",
) -> Document:
    return Document(
        id=chunk_id,
        page_content=content,
        metadata={
            "tenant_id": str(TEST_SCOPE.tenant_id),
            "knowledge_base_id": str(TEST_SCOPE.knowledge_base_id),
            "source": f"C:/docs/{name}.docx",
            "source_id": f"source-{chunk_id}",
            "document_id": str(
                uuid5(NAMESPACE_URL, chunk_id)
            ),
            "document_name": name,
            "section_path": section,
            "chunk_content_hash": f"hash-{chunk_id}",
            "rerank_score": 0.95,
            "retrieval_latency_ms": 120.5,
            "rerank_latency_ms": 800.25,
        },
    )


def build_service(
    monkeypatch,
    *,
    model_result,
    documents: list[Document] | None = None,
):
    retrieval_service = FakeRetrievalService(
        documents if documents is not None else [
            make_document(
                "月度可用性为99.2%时，服务费抵扣比例为10%。"
            )
        ]
    )
    structured_llm = FakeStructuredLLM(model_result)
    fake_chat_model = FakeChatModel(structured_llm)
    constructor_calls: list[dict] = []

    def fake_chat_openai(**kwargs):
        constructor_calls.append(kwargs)
        return fake_chat_model

    monkeypatch.setattr(
        service_module,
        "ChatOpenAI",
        fake_chat_openai,
    )

    settings = SimpleNamespace(
        chat_model="test-model",
        chat_base_url="https://example.com/v1",
        chat_api_key="test-key",
    )
    service = AnswerService(
        settings=settings,
        retrieval_service=retrieval_service,
    )

    return (
        service,
        retrieval_service,
        structured_llm,
        fake_chat_model,
        constructor_calls,
    )


def answer(
    service: AnswerService,
    question: str,
):
    return service.answer(
        question,
        scope=TEST_SCOPE,
        session=TEST_SESSION,
    )


def test_initializes_structured_model_with_expected_settings(
    monkeypatch,
) -> None:
    (
        _service,
        _retrieval,
        _structured_llm,
        chat_model,
        constructor_calls,
    ) = build_service(
        monkeypatch,
        model_result=AnswerDraft(answerable=False),
    )

    assert constructor_calls == [{
        "model": "test-model",
        "base_url": "https://example.com/v1",
        "api_key": "test-key",
        "temperature": 0,
        "reasoning_effort": "none",
        "timeout": 60,
        "max_retries": 0,
    }]
    assert chat_model.schemas == [AnswerDraft]


def test_blank_question_is_rejected_before_retrieval(
    monkeypatch,
) -> None:
    service, retrieval, llm, _, _ = build_service(
        monkeypatch,
        model_result=AnswerDraft(answerable=False),
    )

    with pytest.raises(ValueError, match="问题不能为空"):
        answer(service, "   ")

    assert retrieval.calls == []
    assert llm.calls == []


def test_empty_retrieval_result_returns_refusal_without_model_call(
    monkeypatch,
) -> None:
    service, retrieval, llm, _, _ = build_service(
        monkeypatch,
        model_result=AnswerDraft(answerable=True),
        documents=[],
    )

    result = answer(service, "知识库里有相关制度吗？")

    assert retrieval.calls == [{
        "query": "知识库里有相关制度吗？",
        "scope": TEST_SCOPE,
        "session": TEST_SESSION,
    }]
    assert llm.calls == []
    assert result.answer == REFUSAL_TEXT
    assert result.answerable is False
    assert result.citations == []
    assert result.refusal_reason == "没有检索到相关知识库资料。"
    assert result.timings is not None
    assert result.timings.hybrid_retrieval_ms is None
    assert result.timings.rerank_ms is None
    assert result.timings.generation_ms is None
    assert result.timings.validation_ms is None
    assert result.timings.search_total_ms >= 0
    assert result.timings.context_build_ms >= 0
    assert result.timings.render_ms is not None
    assert result.timings.total_ms >= 0


def test_model_refusal_preserves_reason(monkeypatch) -> None:
    service, _, _, _, _ = build_service(
        monkeypatch,
        model_result=AnswerDraft(
            answerable=False,
            refusal_reason="资料没有说明董事长出生日期。",
        ),
    )

    result = answer(service, "董事长出生日期是什么？")

    assert result.answer == REFUSAL_TEXT
    assert result.answerable is False
    assert result.citations == []
    assert (
        result.refusal_reason
        == "资料没有说明董事长出生日期。"
    )


def test_model_refusal_uses_default_reason(monkeypatch) -> None:
    service, _, _, _, _ = build_service(
        monkeypatch,
        model_result=AnswerDraft(answerable=False),
    )

    result = answer(service, "无法回答的问题")

    assert result.answerable is False
    assert result.refusal_reason == "知识库资料不足。"


def test_valid_claims_render_answer_and_citation_metadata(
    monkeypatch,
) -> None:
    documents = [
        make_document(
            "月度可用性为99.2%时，服务费抵扣比例为10%。",
            chunk_id="chunk-sla",
        ),
        make_document(
            "制度于2026年7月1日生效。",
            name="人事制度",
            section="生效日期",
            chunk_id="chunk-date",
        ),
    ]
    draft = AnswerDraft(
        answerable=True,
        claims=[
            AnswerClaim(
                text="服务费抵扣比例为10%。",
                citations=["资料1"],
            ),
            AnswerClaim(
                text="制度于2026年7月1日生效。",
                citations=["资料2"],
            ),
        ],
    )
    service, retrieval, _, _, _ = build_service(
        monkeypatch,
        model_result=draft,
        documents=documents,
    )

    result = answer(
        service,
        "抵扣比例和制度生效日期是什么？",
    )

    assert retrieval.calls == [{
        "query": "抵扣比例和制度生效日期是什么？",
        "scope": TEST_SCOPE,
        "session": TEST_SESSION,
    }]
    assert result.answerable is True
    assert result.answer == (
        "服务费抵扣比例为10%。[资料1]\n"
        "制度于2026年7月1日生效。[资料2]"
    )
    assert [
        citation.citation_id
        for citation in result.citations
    ] == ["资料1", "资料2"]
    assert result.citations[0].chunk_id == "chunk-sla"
    assert result.citations[0].document_id == uuid5(
        NAMESPACE_URL,
        "chunk-sla",
    )
    assert result.citations[1].document_name == "人事制度"
    assert result.citations[1].section_path == "生效日期"
    assert result.refusal_reason is None
    assert result.timings is not None
    assert result.timings.hybrid_retrieval_ms == 120.5
    assert result.timings.rerank_ms == 800.25
    assert result.timings.search_total_ms >= 0
    assert result.timings.context_build_ms >= 0
    assert result.timings.generation_ms is not None
    assert result.timings.validation_ms is not None
    assert result.timings.render_ms is not None
    assert result.timings.total_ms >= 0


def test_repeated_citation_is_returned_only_once(
    monkeypatch,
) -> None:
    draft = AnswerDraft(
        answerable=True,
        claims=[
            AnswerClaim(
                text="可用性为99.2%",
                citations=["资料1"],
            ),
            AnswerClaim(
                text="抵扣比例为10%",
                citations=["资料1"],
            ),
        ],
    )
    service, _, _, _, _ = build_service(
        monkeypatch,
        model_result=draft,
    )

    result = answer(service, "可用性和抵扣比例是什么？")

    assert result.answerable is True
    assert len(result.citations) == 1
    assert result.citations[0].citation_id == "资料1"


def test_unknown_citation_falls_back_to_refusal(
    monkeypatch,
) -> None:
    service, _, _, _, _ = build_service(
        monkeypatch,
        model_result=AnswerDraft(
            answerable=True,
            claims=[
                AnswerClaim(
                    text="抵扣比例为10%",
                    citations=["资料99"],
                )
            ],
        ),
    )

    result = answer(service, "抵扣比例是什么？")

    assert result.answerable is False
    assert result.answer == REFUSAL_TEXT
    assert "不存在的引用" in result.refusal_reason


def test_unsupported_number_falls_back_to_refusal(
    monkeypatch,
) -> None:
    service, _, _, _, _ = build_service(
        monkeypatch,
        model_result=AnswerDraft(
            answerable=True,
            claims=[
                AnswerClaim(
                    text="抵扣比例为20%",
                    citations=["资料1"],
                )
            ],
        ),
    )

    result = answer(service, "抵扣比例是什么？")

    assert result.answerable is False
    assert "20%" in result.refusal_reason


def test_number_from_question_passes_range_evidence_validation(
    monkeypatch,
) -> None:
    service, _, _, _, _ = build_service(
        monkeypatch,
        model_result=AnswerDraft(
            answerable=True,
            claims=[
                AnswerClaim(
                    text=(
                        "月度可用性为99.2%时，"
                        "服务费抵扣比例为10%"
                    ),
                    citations=["资料1"],
                )
            ],
        ),
        documents=[
            make_document(
                "月度可用性低于99.5%但不低于99.0%时，"
                "服务费抵扣比例为10%。"
            )
        ],
    )

    result = answer(
        service,
        "月度可用性为99.2%时服务抵扣比例是多少？"
    )

    assert result.answerable is True
    assert result.answer == (
        "月度可用性为99.2%时，"
        "服务费抵扣比例为10%。[资料1]"
    )


def test_dictionary_model_result_is_validated_and_rendered(
    monkeypatch,
) -> None:
    service, _, _, _, _ = build_service(
        monkeypatch,
        model_result={
            "answerable": True,
            "claims": [
                {
                    "text": "抵扣比例为10%",
                    "citations": ["资料1"],
                }
            ],
            "refusal_reason": None,
        },
    )

    result = answer(service, "抵扣比例是什么？")

    assert result.answerable is True
    assert result.answer == "抵扣比例为10%。[资料1]"


def test_model_prompt_contains_question_and_built_context(
    monkeypatch,
) -> None:
    service, _, llm, _, _ = build_service(
        monkeypatch,
        model_result=AnswerDraft(
            answerable=False,
            refusal_reason="资料不足",
        ),
    )

    answer(service, "月度可用性是多少？")

    assert len(llm.calls) == 1
    messages = llm.calls[0]
    assert len(messages) == 2
    assert isinstance(messages[0], SystemMessage)
    assert messages[0].content == ANSWER_SYSTEM_PROMPT
    assert isinstance(messages[1], HumanMessage)
    assert "月度可用性是多少？" in messages[1].content
    assert "[资料1]" in messages[1].content
    assert "99.2%" in messages[1].content


@pytest.mark.parametrize("field,value", [("tenant_id", str(uuid4())), ("knowledge_base_id", str(uuid4())),
                                         ("knowledge_base_id", None)])
def test_unscoped_or_unauthorized_evidence_never_enters_model(monkeypatch, field, value):
    document = make_document("不应发送的敏感资料")
    document.metadata[field] = value
    service, _, llm, _, _ = build_service(monkeypatch, model_result=AnswerDraft(answerable=True), documents=[document])
    result = answer(service, "问题")
    assert result.answerable is False
    assert result.citations == []
    assert llm.calls == []


def test_cross_kb_answer_citations_and_context_preserve_source_scope(monkeypatch):
    kb_id = uuid4()
    scope = RetrievalScope(TEST_SCOPE.tenant_id, knowledge_base_ids={TEST_SCOPE.knowledge_base_id, kb_id})
    documents = [make_document("条件甲", chunk_id="one"), make_document("条件乙", chunk_id="two")]
    documents[0].metadata["knowledge_base_name"] = "公司公共知识库"
    documents[1].metadata.update(knowledge_base_id=str(kb_id), knowledge_base_name="技术部知识库")
    service, _, llm, _, _ = build_service(monkeypatch, documents=documents, model_result=AnswerDraft(
        answerable=True, claims=[AnswerClaim(text="需要满足条件甲和条件乙", citations=["资料1", "资料2"])],
    ))
    result = service.answer("需要什么条件？", scope=scope, session=TEST_SESSION)
    assert result.answerable is True
    assert [c.knowledge_base_id for c in result.citations] == [TEST_SCOPE.knowledge_base_id, kb_id]
    assert [c.knowledge_base_name for c in result.citations] == ["公司公共知识库", "技术部知识库"]
    assert "知识库：技术部知识库" in llm.calls[0][1].content


@pytest.mark.parametrize("proposed,answerable", [("130.30", True), ("999", False)])
def test_calculation_is_program_rendered_traced_and_verified_once_by_model(monkeypatch, proposed, answerable):
    from backend.app.evaluation.trace import capture_trace
    draft = AnswerDraft.model_validate({"answerable": True, "claims": [{
        "text": "模型附带了未经支持的描述999999元", "citations": ["资料1"],
        "calculation": {"operation": "money_sum", "result": proposed, "inputs": [
            {"source": "资料1", "quote": "100.20元", "value": "100.20", "unit": "元"},
            {"source": "资料1", "quote": "30.10元", "value": "30.10", "unit": "元"}]}}]})
    service, _, llm, _, _ = build_service(monkeypatch, model_result=draft,
                                         documents=[make_document("项目甲100.20元；项目乙30.10元。")])
    with capture_trace() as trace:
        result = answer(service, "这两个项目金额合计是多少？")
    assert result.answerable is answerable
    assert len(llm.calls) == 1
    assert "999999" not in result.answer
    record = trace.stages["calculations"][0]
    if answerable:
        assert "100.20元 + 30.10元 = 130.3元" in result.answer
        assert result.citations[0].citation_id == "资料1"
        assert record["status"] == "verified" and record["result"] == "130.3"
    else:
        assert "程序复算不一致" in result.refusal_reason
        assert result.citations == [] and record["status"] == "rejected"


def test_header_first_documents_send_rule_body_to_model_and_preserve_citation(monkeypatch):
    from backend.app.evaluation.trace import capture_trace
    texts = ["投递规范", "示例科技有限公司", "版本：1.0；生效日期：2026年10月1日。",
             "虚构测试资料，仅用于开发，不具有实际法律效力。",
             "首次投递后HTTP 429或5xx触发重试；其他4xx直接进入失败待核查。"]
    docs = [make_document("文档：投递规范\n章节：" + ("文档说明" if i < 4 else "成功与重试")
                         + "\n\n" + text, name="投递规范", section="" if i < 4 else "成功与重试",
                         chunk_id=f"part-{i}") for i, text in enumerate(texts)]
    for doc in docs:
        doc.metadata["source_id"] = "shared-source"
    service, _, llm, _, _ = build_service(monkeypatch, documents=docs, model_result=AnswerDraft(
        answerable=True, claims=[AnswerClaim(text="401属于其他4xx，进入失败待核查。", citations=["资料1"])]))
    with capture_trace() as trace:
        result = answer(service, "Webhook返回401状态码时，会自动重试吗？")
    assert "其他4xx直接进入失败待核查" in llm.calls[0][1].content
    assert len(llm.calls) == 1 and result.answerable
    assert result.citations[0].chunk_id == "part-4"
    assert trace.stages["context"][0]["evidence_role"] == "content"
