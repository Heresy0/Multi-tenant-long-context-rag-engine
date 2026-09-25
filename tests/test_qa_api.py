from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.app.answer_models import (
    AnswerResult,
    AnswerTimings,
    Citation,
)
from backend.app.api.qa import router


class FakeAnswerService:
    def __init__(
        self,
        *,
        result: AnswerResult | None = None,
        error: Exception | None = None,
    ) -> None:
        self.result = result
        self.error = error
        self.questions: list[str] = []

    def answer(self, question: str) -> AnswerResult:
        self.questions.append(question)

        if self.error is not None:
            raise self.error

        if self.result is None:
            raise AssertionError("测试未配置回答结果")

        return self.result


def create_test_client(
    answer_service: FakeAnswerService,
) -> TestClient:
    # 使用独立应用，不执行正式项目的 lifespan，
    # 因而不会连接真实模型、Chroma 或 SQLite。
    app = FastAPI()
    app.state.answer_service = answer_service
    app.include_router(router)

    return TestClient(app)


def test_qa_returns_answer_with_citations() -> None:
    service = FakeAnswerService(
        result=AnswerResult(
            answer="抵扣比例为10%。[资料1]",
            answerable=True,
            timings=AnswerTimings(
                hybrid_retrieval_ms=120.5,
                rerank_ms=800.25,
                search_total_ms=925.0,
                context_build_ms=0.5,
                generation_ms=9000.0,
                validation_ms=0.3,
                render_ms=0.2,
                total_ms=9926.0,
            ),
            citations=[
                Citation(
                    citation_id="资料1",
                    document_name="服务等级协议",
                    section_path="服务抵扣",
                    source="C:/docs/sla.docx",
                    chunk_id="chunk-1",
                )
            ],
        )
    )
    client = create_test_client(service)

    response = client.post(
        "/api/qa",
        json={
            "question": "抵扣比例是多少？",
        },
    )

    assert response.status_code == 200
    assert service.questions == ["抵扣比例是多少？"]
    assert response.json() == {
        "answer": "抵扣比例为10%。[资料1]",
        "answerable": True,
        "citations": [
            {
                "citation_id": "资料1",
                "document_name": "服务等级协议",
                "section_path": "服务抵扣",
                "source": "C:/docs/sla.docx",
                "chunk_id": "chunk-1",
            }
        ],
        "refusal_reason": None,
        "timings": {
            "hybrid_retrieval_ms": 120.5,
            "rerank_ms": 800.25,
            "search_total_ms": 925.0,
            "context_build_ms": 0.5,
            "generation_ms": 9000.0,
            "validation_ms": 0.3,
            "render_ms": 0.2,
            "total_ms": 9926.0,
        },
    }


def test_qa_returns_normal_refusal_with_http_200() -> None:
    service = FakeAnswerService(
        result=AnswerResult(
            answer=(
                "根据当前知识库资料，"
                "暂时无法回答这个问题。"
            ),
            answerable=False,
            citations=[],
            refusal_reason="资料没有说明该信息。",
        )
    )
    client = create_test_client(service)

    response = client.post(
        "/api/qa",
        json={
            "question": "董事长出生日期是什么？",
        },
    )

    assert response.status_code == 200
    assert response.json()["answerable"] is False
    assert response.json()["citations"] == []
    assert (
        response.json()["refusal_reason"]
        == "资料没有说明该信息。"
    )


def test_qa_converts_blank_question_error_to_422() -> None:
    service = FakeAnswerService(
        error=ValueError("问题不能为空")
    )
    client = create_test_client(service)

    response = client.post(
        "/api/qa",
        json={"question": "   "},
    )

    assert response.status_code == 422
    assert response.json() == {
        "detail": "问题不能为空"
    }
    assert service.questions == ["   "]


def test_qa_rejects_missing_question_before_service_call() -> None:
    service = FakeAnswerService()
    client = create_test_client(service)

    response = client.post(
        "/api/qa",
        json={},
    )

    assert response.status_code == 422
    assert service.questions == []


def test_qa_rejects_empty_question_before_service_call() -> None:
    service = FakeAnswerService()
    client = create_test_client(service)

    response = client.post(
        "/api/qa",
        json={"question": ""},
    )

    assert response.status_code == 422
    assert service.questions == []


def test_qa_rejects_question_over_maximum_length() -> None:
    service = FakeAnswerService()
    client = create_test_client(service)

    response = client.post(
        "/api/qa",
        json={"question": "问" * 1001},
    )

    assert response.status_code == 422
    assert service.questions == []


def test_qa_converts_unexpected_service_error_to_503() -> None:
    service = FakeAnswerService(
        error=RuntimeError("upstream unavailable")
    )
    client = create_test_client(service)

    response = client.post(
        "/api/qa",
        json={
            "question": "报销流程是什么？",
        },
    )

    assert response.status_code == 503
    assert response.json() == {
        "detail": "知识库问答服务暂时不可用。"
    }
    assert service.questions == ["报销流程是什么？"]
