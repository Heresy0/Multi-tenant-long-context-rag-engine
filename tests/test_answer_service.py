from types import SimpleNamespace

from langchain_core.documents import Document
from langchain_core.messages import (
    HumanMessage,
    SystemMessage,
)
import pytest

from backend.app import answer_service as service_module
from backend.app.answer_models import (
    AnswerClaim,
    AnswerDraft,
)
from backend.app.answer_service import (
    REFUSAL_TEXT,
    AnswerService,
)
from backend.app.prompts import ANSWER_SYSTEM_PROMPT


class FakeRetrievalService:
    def __init__(
        self,
        documents: list[Document],
    ) -> None:
        self.documents = documents
        self.queries: list[str] = []

    def search(self, query: str) -> list[Document]:
        self.queries.append(query)
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
            "source": f"C:/docs/{name}.docx",
            "source_id": f"source-{chunk_id}",
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
        "timeout": 60,
        "max_retries": 1,
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
        service.answer("   ")

    assert retrieval.queries == []
    assert llm.calls == []


def test_empty_retrieval_result_returns_refusal_without_model_call(
    monkeypatch,
) -> None:
    service, retrieval, llm, _, _ = build_service(
        monkeypatch,
        model_result=AnswerDraft(answerable=True),
        documents=[],
    )

    result = service.answer("知识库里有相关制度吗？")

    assert retrieval.queries == ["知识库里有相关制度吗？"]
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

    result = service.answer("董事长出生日期是什么？")

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

    result = service.answer("无法回答的问题")

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

    result = service.answer("抵扣比例和制度生效日期是什么？")

    assert retrieval.queries == [
        "抵扣比例和制度生效日期是什么？"
    ]
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

    result = service.answer("可用性和抵扣比例是什么？")

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

    result = service.answer("抵扣比例是什么？")

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

    result = service.answer("抵扣比例是什么？")

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

    result = service.answer(
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

    result = service.answer("抵扣比例是什么？")

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

    service.answer("月度可用性是多少？")

    assert len(llm.calls) == 1
    messages = llm.calls[0]
    assert len(messages) == 2
    assert isinstance(messages[0], SystemMessage)
    assert messages[0].content == ANSWER_SYSTEM_PROMPT
    assert isinstance(messages[1], HumanMessage)
    assert "月度可用性是多少？" in messages[1].content
    assert "[资料1]" in messages[1].content
    assert "99.2%" in messages[1].content
