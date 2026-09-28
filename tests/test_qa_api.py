from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.app.api import qa as qa_module
from backend.app.answer_models import (
    AnswerResult,
    AnswerTimings,
    Citation,
)
from backend.app.api.qa import router
from backend.app.db.dependencies import get_database_session
from backend.app.security.authorization import (
    AuthorizationDenied,
)
from backend.app.security.dependencies import (
    get_current_principal,
)
from backend.app.security.principal import Principal
from backend.app.security.retrieval_scope import (
    RetrievalScope,
)


TENANT_ID = uuid4()
USER_ID = uuid4()
KNOWLEDGE_BASE_ID = uuid4()
TEST_PRINCIPAL = Principal(
    user_id=USER_ID,
    tenant_id=TENANT_ID,
    external_subject="alice",
)
TEST_SCOPE = RetrievalScope(
    tenant_id=TENANT_ID,
    knowledge_base_id=KNOWLEDGE_BASE_ID,
)


class FakeAnswerService:
    def __init__(
        self,
        *,
        result: AnswerResult | None = None,
        error: Exception | None = None,
    ) -> None:
        self.result = result
        self.error = error
        self.calls: list[dict] = []

    def answer(
        self,
        question: str,
        *,
        scope: RetrievalScope,
        session: object,
    ) -> AnswerResult:
        self.calls.append({
            "question": question,
            "scope": scope,
            "session": session,
        })

        if self.error is not None:
            raise self.error

        if self.result is None:
            raise AssertionError("测试未配置回答结果")

        return self.result


class FakeAuthorizationService:
    def __init__(
        self,
        *,
        scope: RetrievalScope,
        error: Exception | None = None,
    ) -> None:
        self.scope = scope
        self.error = error
        self.sessions: list[object] = []
        self.calls: list[dict] = []

    def require_retrieval_scope(
        self,
        *,
        principal: Principal,
        knowledge_base_id,
    ) -> RetrievalScope:
        self.calls.append({
            "principal": principal,
            "knowledge_base_id": knowledge_base_id,
        })

        if self.error is not None:
            raise self.error

        return self.scope


def create_test_client(
    monkeypatch,
    answer_service: FakeAnswerService,
    *,
    authorization_error: Exception | None = None,
    authenticated: bool = True,
) -> tuple[TestClient, FakeAuthorizationService, object]:
    # 使用独立应用，不执行正式项目的 lifespan，
    # 因而不会连接真实模型或数据库。
    app = FastAPI()
    app.state.answer_service = answer_service
    app.include_router(router)
    session = object()
    authorization = FakeAuthorizationService(
        scope=TEST_SCOPE,
        error=authorization_error,
    )

    app.dependency_overrides[
        get_database_session
    ] = lambda: session

    if authenticated:
        app.dependency_overrides[
            get_current_principal
        ] = lambda: TEST_PRINCIPAL

    def create_authorization_service(
        actual_session: object,
    ) -> FakeAuthorizationService:
        authorization.sessions.append(actual_session)
        return authorization

    monkeypatch.setattr(
        qa_module,
        "AuthorizationService",
        create_authorization_service,
    )

    return TestClient(app), authorization, session


def test_qa_returns_answer_with_citations(
    monkeypatch,
) -> None:
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
    client, authorization, session = create_test_client(
        monkeypatch,
        service,
    )

    response = client.post(
        "/api/qa",
        json={
            "knowledge_base_id": str(KNOWLEDGE_BASE_ID),
            "question": "抵扣比例是多少？",
        },
    )

    assert response.status_code == 200
    assert authorization.sessions == [session]
    assert authorization.calls == [{
        "principal": TEST_PRINCIPAL,
        "knowledge_base_id": KNOWLEDGE_BASE_ID,
    }]
    assert service.calls == [{
        "question": "抵扣比例是多少？",
        "scope": TEST_SCOPE,
        "session": session,
    }]
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


def test_qa_returns_normal_refusal_with_http_200(
    monkeypatch,
) -> None:
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
    client, _, _ = create_test_client(
        monkeypatch,
        service,
    )

    response = client.post(
        "/api/qa",
        json={
            "knowledge_base_id": str(KNOWLEDGE_BASE_ID),
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


def test_qa_converts_blank_question_error_to_422(
    monkeypatch,
) -> None:
    service = FakeAnswerService(
        error=ValueError("问题不能为空")
    )
    client, _, session = create_test_client(
        monkeypatch,
        service,
    )

    response = client.post(
        "/api/qa",
        json={
            "knowledge_base_id": str(KNOWLEDGE_BASE_ID),
            "question": "   ",
        },
    )

    assert response.status_code == 422
    assert response.json() == {
        "detail": "问题不能为空"
    }
    assert service.calls == [{
        "question": "   ",
        "scope": TEST_SCOPE,
        "session": session,
    }]


def test_qa_rejects_missing_question_before_service_call(
    monkeypatch,
) -> None:
    service = FakeAnswerService()
    client, authorization, _ = create_test_client(
        monkeypatch,
        service,
    )

    response = client.post(
        "/api/qa",
        json={
            "knowledge_base_id": str(KNOWLEDGE_BASE_ID),
        },
    )

    assert response.status_code == 422
    assert authorization.calls == []
    assert service.calls == []


def test_qa_rejects_empty_question_before_service_call(
    monkeypatch,
) -> None:
    service = FakeAnswerService()
    client, authorization, _ = create_test_client(
        monkeypatch,
        service,
    )

    response = client.post(
        "/api/qa",
        json={
            "knowledge_base_id": str(KNOWLEDGE_BASE_ID),
            "question": "",
        },
    )

    assert response.status_code == 422
    assert authorization.calls == []
    assert service.calls == []


def test_qa_rejects_question_over_maximum_length(
    monkeypatch,
) -> None:
    service = FakeAnswerService()
    client, authorization, _ = create_test_client(
        monkeypatch,
        service,
    )

    response = client.post(
        "/api/qa",
        json={
            "knowledge_base_id": str(KNOWLEDGE_BASE_ID),
            "question": "问" * 1001,
        },
    )

    assert response.status_code == 422
    assert authorization.calls == []
    assert service.calls == []


def test_qa_converts_unexpected_service_error_to_503(
    monkeypatch,
) -> None:
    service = FakeAnswerService(
        error=RuntimeError("upstream unavailable")
    )
    client, _, session = create_test_client(
        monkeypatch,
        service,
    )

    response = client.post(
        "/api/qa",
        json={
            "knowledge_base_id": str(KNOWLEDGE_BASE_ID),
            "question": "报销流程是什么？",
        },
    )

    assert response.status_code == 503
    assert response.json() == {
        "detail": "知识库问答服务暂时不可用。"
    }
    assert service.calls == [{
        "question": "报销流程是什么？",
        "scope": TEST_SCOPE,
        "session": session,
    }]


def test_qa_requires_authentication(monkeypatch) -> None:
    service = FakeAnswerService()
    client, authorization, _ = create_test_client(
        monkeypatch,
        service,
        authenticated=False,
    )

    response = client.post(
        "/api/qa",
        json={
            "knowledge_base_id": str(KNOWLEDGE_BASE_ID),
            "question": "技术手册讲了什么？",
        },
    )

    assert response.status_code == 401
    assert response.json()["detail"] == "未认证。"
    assert authorization.calls == []
    assert service.calls == []


def test_qa_rejects_unauthorized_knowledge_base(
    monkeypatch,
) -> None:
    service = FakeAnswerService()
    client, authorization, _ = create_test_client(
        monkeypatch,
        service,
        authorization_error=AuthorizationDenied(
            "没有权限访问该知识库。"
        ),
    )

    response = client.post(
        "/api/qa",
        json={
            "knowledge_base_id": str(KNOWLEDGE_BASE_ID),
            "question": "人力资源制度是什么？",
        },
    )

    assert response.status_code == 403
    assert response.json() == {
        "detail": "没有权限访问该知识库。"
    }
    assert authorization.calls == [{
        "principal": TEST_PRINCIPAL,
        "knowledge_base_id": KNOWLEDGE_BASE_ID,
    }]
    assert service.calls == []
