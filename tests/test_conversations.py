import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from backend.app.api import conversations as api_module, qa as qa_module
from backend.app.db.base import Base
from backend.app.db.conversation_models import Conversation, ConversationTurn
from backend.app.db.dependencies import get_database_session
from backend.app.db.models import KnowledgeBase, Tenant, User
from backend.app.knowledge.answer_models import AnswerResult
from backend.app.knowledge.conversation_service import ConversationAnswerService, ConversationResolver, ResolvedQuestion
from backend.app.knowledge.conversations import ConversationBusy, ConversationNotFound, ConversationStore, HISTORY_TURNS
from backend.app.security.dependencies import get_current_principal
from backend.app.security.principal import Principal
from backend.app.security.retrieval_scope import RetrievalScope


@pytest.fixture
def data():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)

    @event.listens_for(engine, "connect")
    def foreign_keys(conn, _):
        conn.execute("PRAGMA foreign_keys = ON")

    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        tenant, other_tenant = Tenant(name="A"), Tenant(name="B")
        session.add_all([tenant, other_tenant]); session.flush()
        user = User(tenant_id=tenant.id, name="Alice", external_subject="alice")
        other_user = User(tenant_id=tenant.id, name="Bob", external_subject="bob")
        kb = KnowledgeBase(tenant_id=tenant.id, name="KB", visibility="company")
        other_kb = KnowledgeBase(tenant_id=tenant.id, name="Other", visibility="company")
        session.add_all([user, other_user, kb, other_kb]); session.commit()
        principal = Principal(user.id, tenant.id, "alice")
        scope = RetrievalScope(tenant.id, kb.id)
        yield SimpleNamespace(session=session, engine=engine, principal=principal, scope=scope,
                              other_user=other_user, other_tenant=other_tenant, other_kb=other_kb,
                              store=ConversationStore(session, principal, scope))
    engine.dispose()


def result():
    return AnswerResult(answer="有效期为 2 小时。", answerable=True, citations=[])


class Resolver:
    def __init__(self):
        self.histories = []
        self.error = None

    def resolve(self, question, turns):
        self.histories.append(turns)
        if self.error:
            raise self.error
        return "开放平台访问令牌过期后如何处理？" if turns else question


class Answer:
    def __init__(self):
        self.questions = []
        self.error = None

    def answer(self, question, *, scope, session):
        self.questions.append(question)
        if self.error:
            raise self.error
        return result()


def test_paired_turns_persist_and_followup_uses_standalone_question(data):
    conversation = data.store.create()
    resolver, answer = Resolver(), Answer()
    service = ConversationAnswerService(answer, resolver)
    for question in ["开放平台令牌有效期是多少？", "它过期了怎么办？"]:
        service.answer(question, conversation_id=conversation.id, principal=data.principal,
                       scope=data.scope, session=data.session)
    assert answer.questions == ["开放平台令牌有效期是多少？", "开放平台访问令牌过期后如何处理？"]
    assert len(resolver.histories[1]) == 1
    assert resolver.histories[1][0].question == answer.questions[0]
    # Reopen with an independent session, as after a restart/page reload.
    with Session(data.engine) as session:
        store = ConversationStore(session, data.principal, data.scope)
        turns = store.turns(conversation.id)
        assert [t.turn_index for t in turns] == [1, 2]
        assert turns[1].question == "它过期了怎么办？"
        assert turns[1].retrieval_question == answer.questions[1]
        assert turns[0].result_json == result().model_dump(mode="json")
        assert store.get(conversation.id).turn_count == 2


def local_evaluation_fixture(data, monkeypatch):
    from langchain_core.documents import Document
    from backend.app.evaluation import conversation_runner as runner
    from backend.app.evaluation.trace import record_scope, record_documents, record_context
    from backend.app.knowledge.context_builder import ContextBuilder
    from backend.app.knowledge.answer_models import Citation
    monkeypatch.setenv('LOCAL_TEST_TOKEN', 'a.b.c')
    monkeypatch.setattr(runner, 'Settings', lambda: SimpleNamespace(database_url='sqlite://'))
    monkeypatch.setattr(runner, 'create_database_engine', lambda _: data.engine)
    monkeypatch.setattr(data.engine, 'dispose', lambda: None)
    verified = []
    def verify(settings, session, token):
        verified.append(token)
        return data.principal
    monkeypatch.setattr(runner, 'verified_principal', verify)
    resolver, injected_error = Resolver(), []
    class RecordingAnswer:
        def answer(self, question, *, scope, session):
            if injected_error:
                raise RuntimeError('Never print Bearer secret-token or postgres://secret')
            record_scope(question, scope)
            doc_id = uuid4()
            doc = Document(id='evidence', page_content='有效期为2小时。', metadata=dict(
                document_id=str(doc_id), document_name='规范', tenant_id=str(scope.tenant_id),
                knowledge_base_id=str(data.scope.knowledge_base_id)))
            record_documents('authorized_final', [doc])
            context = ContextBuilder().build([doc], question=question)
            record_context(context)
            return AnswerResult(answer='有效期为2小时。', answerable=True, citations=[Citation(
                citation_id='资料1', document_id=doc_id, document_name='规范', section_path='未标注章节',
                chunk_id='evidence', knowledge_base_id=data.scope.knowledge_base_id)])
    monkeypatch.setattr(runner, 'RetrievalService', lambda _: object())
    monkeypatch.setattr(runner, 'AnswerService', lambda **_: RecordingAnswer())
    monkeypatch.setattr(runner, 'ConversationResolver', lambda _: resolver)
    config = dict(knowledge_bases={'公共': str(data.scope.knowledge_base_id), '其他': str(data.other_kb.id)},
                  profiles={'全范围测试用户': dict(token_env='LOCAL_TEST_TOKEN', expected_groups=['公共', '其他'])})
    cases = [dict(id='MT-local', turns=[dict(id=f'T{i}', question=q, required_facts=['2小时'])
                    for i, q in enumerate(('有效期？', '它过期了怎么办？'))])]
    return runner, config, cases, verified, injected_error


def test_local_multi_turn_traces_are_same_pipeline_and_cleanup_only_own_session(data, monkeypatch, tmp_path):
    runner, config, cases, verified, _ = local_evaluation_fixture(data, monkeypatch)
    existing = data.store.create().id
    path = tmp_path / 'turns.jsonl'
    report = runner.evaluate_local_conversations(cases, config, save_traces=path)
    assert report['summary']['passed'] == 1 and report['turn_summary']['passed'] == 2
    assert len(verified) == 3  # Setup + both turns; not just one identity check.
    assert report['evaluation_type'] == 'authenticated_local_conversation_service_not_http'
    traces = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]
    assert len(traces) == 2 and traces[1]['stages']['conversation_resolution'][0]['history_count'] == 1
    assert traces[1]['query'] == '开放平台访问令牌过期后如何处理？'
    assert traces[1]['stages']['context'][0]['content'] == '有效期为2小时。'
    assert traces[1]['response']['citations'][0]['chunk_id'] == 'evidence'
    assert report['records'][0]['cleanup'] == 'deleted_owned_evaluation_conversation'
    assert [row.id for row in data.session.scalars(select(Conversation))] == [existing]


def test_local_multi_turn_error_is_redacted_later_turn_skipped_and_owned_session_cleaned(data, monkeypatch, tmp_path):
    runner, config, cases, _, injected_error = local_evaluation_fixture(data, monkeypatch)
    injected_error.append(True)
    path = tmp_path / 'turns.jsonl'
    report = runner.evaluate_local_conversations(cases, config, save_traces=path)
    assert [row['status'] for row in report['records'][0]['turns']] == ['error', 'skipped']
    assert report['records'][0]['cleanup'] == 'deleted_owned_evaluation_conversation'
    assert not data.session.scalars(select(Conversation)).all()
    text = path.read_text(encoding='utf-8') + json.dumps(report)
    assert 'secret-token' not in text and 'postgres://' not in text


def test_local_multi_turn_scope_mismatch_prevents_session_creation(data, monkeypatch):
    runner, config, cases, _, _ = local_evaluation_fixture(data, monkeypatch)
    config['profiles']['全范围测试用户']['expected_groups'] = ['公共']
    report = runner.evaluate_local_conversations(cases, config)
    assert report['summary']['error'] == 1 and not report['records'][0]['turns']
    assert not data.session.scalars(select(Conversation)).all()


def test_local_multi_turn_never_overwrites_existing_snapshot(data, monkeypatch, tmp_path):
    runner, config, cases, _, _ = local_evaluation_fixture(data, monkeypatch)
    path = tmp_path / 'turns.jsonl'
    path.write_text('existing', encoding='utf-8')
    with pytest.raises(FileExistsError):
        runner.evaluate_local_conversations(cases, config, save_traces=path)
    assert path.read_text(encoding='utf-8') == 'existing'


@pytest.mark.parametrize("change", ["user", "tenant", "kb"])
def test_private_conversation_isolation(data, change):
    conversation = data.store.create()
    principal, scope = data.principal, data.scope
    if change == "user":
        principal = Principal(data.other_user.id, principal.tenant_id, "bob")
    elif change == "tenant":
        principal = Principal(uuid4(), data.other_tenant.id, "other")
        scope = RetrievalScope(data.other_tenant.id, uuid4())
    else:
        scope = RetrievalScope(scope.tenant_id, data.other_kb.id)
    store = ConversationStore(data.session, principal, scope)
    assert store.list() == []
    for action in [store.get, store.turns, store.acquire, store.delete]:
        with pytest.raises(ConversationNotFound):
            action(conversation.id)


def test_database_rejects_cross_tenant_owner_and_cross_kb_turn(data):
    conversation = data.store.create()
    data.session.add(Conversation(tenant_id=data.other_tenant.id, user_id=data.principal.user_id,
                                  knowledge_base_id=data.scope.knowledge_base_id))
    with pytest.raises(IntegrityError):
        data.session.commit()
    data.session.rollback()
    data.session.add(ConversationTurn(tenant_id=data.scope.tenant_id, knowledge_base_id=data.other_kb.id,
        conversation_id=conversation.id, turn_index=1, question="q", retrieval_question="q", result_json={}))
    with pytest.raises(IntegrityError):
        data.session.commit()
    data.session.rollback()


def test_concurrent_request_and_delete_are_rejected_then_expired_lease_recovers(data):
    conversation = data.store.create()
    token = data.store.acquire(conversation.id)
    with Session(data.engine) as session:
        other = ConversationStore(session, data.principal, data.scope)
        with pytest.raises(ConversationBusy):
            other.acquire(conversation.id)
        with pytest.raises(ConversationBusy):
            other.delete(conversation.id)
    data.session.execute(update(Conversation).where(Conversation.id == conversation.id).values(
        lease_expires_at=datetime.now(timezone.utc) - timedelta(seconds=1)))
    data.session.commit()
    replacement = data.store.acquire(conversation.id)
    assert replacement != token
    data.store.release(conversation.id, token)
    assert data.store.get(conversation.id).lease_token == replacement
    with pytest.raises(ConversationBusy):
        data.store.append(conversation.id, token, "q", "q", result())
    data.store.append(conversation.id, replacement, "q", "q", result())


@pytest.mark.parametrize("stage", ["resolver", "answer", "audit"])
def test_failed_requests_leave_no_turn_and_release_lease(data, stage):
    conversation = data.store.create()
    resolver, answer = Resolver(), Answer()
    callback = None
    if stage == "audit":
        def callback(_):
            raise RuntimeError("audit failure")
    else:
        getattr(SimpleNamespace(resolver=resolver, answer=answer), stage).error = RuntimeError("unavailable")
    service = ConversationAnswerService(answer, resolver)
    with pytest.raises(RuntimeError):
        service.answer("问题", conversation_id=conversation.id, principal=data.principal,
                       scope=data.scope, session=data.session, commit_success=callback)
    assert data.store.turns(conversation.id) == []
    assert data.store.get(conversation.id).turn_count == 0
    assert data.store.get(conversation.id).lease_token is None
    assert data.store.acquire(conversation.id)


def test_history_window_and_pagination_keep_all_stored_turns(data):
    conversation = data.store.create()
    resolver = Resolver()
    service = ConversationAnswerService(Answer(), resolver)
    for i in range(9):
        service.answer(f"问题{i}", conversation_id=conversation.id, principal=data.principal,
                       scope=data.scope, session=data.session)
    assert len(resolver.histories[-1]) == HISTORY_TURNS
    assert [t.turn_index for t in resolver.histories[-1]] == [3, 4, 5, 6, 7, 8]
    assert len(data.store.turns(conversation.id)) == 9
    assert [t.turn_index for t in data.store.turns(conversation.id, limit=2, before=5)] == [3, 4]
    data.store.delete(conversation.id)
    assert data.session.scalars(select(ConversationTurn)).all() == []


def test_resolver_bounds_untrusted_context_and_skips_llm_on_first_turn(monkeypatch):
    calls = []
    class Model:
        def with_structured_output(self, schema):
            assert schema is ResolvedQuestion
            return self
        def invoke(self, messages):
            calls.append(messages)
            return ResolvedQuestion(question="访问令牌过期后如何处理？")
    monkeypatch.setattr("backend.app.knowledge.conversation_service.ChatOpenAI", lambda **_: Model())
    resolver = ConversationResolver(SimpleNamespace(chat_model="fake", chat_base_url="http://unused", chat_api_key="fake"))
    assert resolver.resolve("令牌有效期？", []) == "令牌有效期？"
    assert calls == []
    turns = [SimpleNamespace(question="q" * 2000, retrieval_question="r" * 2000,
                              result_json={"answer": "a" * 3000, "answerable": True}) for _ in range(20)]
    assert resolver.resolve("过期后怎么办？", turns) == "访问令牌过期后如何处理？"
    history = json.loads(calls[0][1].content)["history"]
    assert len(history) == 6
    assert len(history[0]["question"]) == 1000
    assert len(history[0]["answer"]) == 1200
    assert "不把历史回答当成事实证据" in calls[0][0].content


def test_empty_or_invalid_rewrite_fails_without_persisting(data):
    conversation = data.store.create()
    resolver = Resolver()
    resolver.resolve = lambda *_: " "
    with pytest.raises(ValueError):
        ConversationAnswerService(Answer(), resolver).answer("追问", conversation_id=conversation.id,
            principal=data.principal, scope=data.scope, session=data.session)
    assert data.store.turns(conversation.id) == []


def test_refusal_is_a_completed_turn_and_keeps_refusal_metadata(data):
    conversation = data.store.create()
    refusal = AnswerResult(answer="资料不足，无法回答。", answerable=False, citations=[], refusal_reason="没有相关证据")
    answer = Answer()
    answer.answer = lambda *args, **kwargs: refusal
    ConversationAnswerService(answer, Resolver()).answer("资料没有的问题", conversation_id=conversation.id,
        principal=data.principal, scope=data.scope, session=data.session)
    saved = data.store.turns(conversation.id)[0].result_json
    assert saved["answerable"] is False
    assert saved["refusal_reason"] == "没有相关证据"


def test_migration_upgrade_and_downgrade_match_orm(data):
    import importlib
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import inspect
    migration = importlib.import_module("migrations.versions.f3a5b7c9d204_add_conversations")
    all_scope_migration = importlib.import_module("migrations.versions.b8d0f2a4c607_all_accessible_conversations")
    data.session.close()
    with data.engine.begin() as connection:
        ConversationTurn.__table__.drop(connection)
        Conversation.__table__.drop(connection)
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
            all_scope_migration.upgrade()
        schema = inspect(connection)
        for model in (Conversation, ConversationTurn):
            assert {c["name"] for c in schema.get_columns(model.__tablename__)} == set(model.__table__.columns.keys())
            assert {c["name"] for c in schema.get_foreign_keys(model.__tablename__)} == {
                c.name for c in model.__table__.foreign_key_constraints
            }
        with Operations.context(MigrationContext.configure(connection)):
            all_scope_migration.downgrade()
            migration.downgrade()
        assert "conversations" not in inspect(connection).get_table_names()
        assert "conversation_turns" not in inspect(connection).get_table_names()


@pytest.fixture
def client(data):
    app = FastAPI()
    app.include_router(api_module.router)
    app.include_router(qa_module.router)
    app.state.answer_service = Answer()
    app.state.conversation_answer_service = ConversationAnswerService(app.state.answer_service, Resolver())
    app.state.settings = SimpleNamespace(qa_rate_limit_requests=60, qa_rate_limit_window_seconds=60,
        qa_max_concurrent_requests_per_tenant=3, qa_concurrency_lease_seconds=180)
    from backend.app.governance.tenant_concurrency_limiter import ConcurrencyDecision, ConcurrencyLease
    from backend.app.governance.tenant_rate_limiter import RateLimitDecision
    app.state.tenant_rate_limiter = SimpleNamespace(consume=lambda **_: RateLimitDecision(True, 60, 59, 60))
    app.state.tenant_concurrency_limiter = SimpleNamespace(
        acquire=lambda **_: ConcurrencyDecision(True, 3, 1, 2, 0, ConcurrencyLease(data.principal.tenant_id, "qa", "token")),
        release=lambda _: True,
    )
    app.dependency_overrides[get_database_session] = lambda: data.session
    app.dependency_overrides[get_current_principal] = lambda: data.principal
    with TestClient(app) as test_client:
        yield test_client


def test_api_create_continue_restore_list_delete_and_stateless_compatibility(data, client):
    path = f"/api/knowledge-bases/{data.scope.knowledge_base_id}/conversations"
    response = client.post(path)
    assert response.status_code == 201, response.text
    cid = response.json()["id"]
    payload = {"knowledge_base_id": str(data.scope.knowledge_base_id), "question": "令牌有效期？"}
    stateless = client.post("/api/qa", json=payload)
    assert stateless.status_code == 200, stateless.text
    assert "conversation_id" not in stateless.json()
    for question in ["令牌有效期？", "它过期怎么办？"]:
        response = client.post("/api/qa", json={**payload, "question": question, "conversation_id": cid})
        assert response.status_code == 200, response.text
        assert response.json()["conversation_id"] == cid
        assert response.json()["conversation_context_ms"] >= 0
    detail = client.get(f"{path}/{cid}").json()
    assert detail["turn_count"] == 2
    assert detail["turns"][1]["retrieval_question"] == "开放平台访问令牌过期后如何处理？"
    assert detail["turns"][0]["result"]["answer"] == result().answer
    assert client.get(path).json()["items"][0]["title"] == "令牌有效期？"
    paged = client.get(f"{path}/{cid}?limit=1").json()
    assert paged["next_before"] == 2
    assert client.get(f"{path}/{cid}?before=2").json()["turns"][0]["turn_index"] == 1
    assert client.delete(f"{path}/{cid}").status_code == 204
    assert client.get(f"{path}/{cid}").status_code == 404


def test_api_other_user_cannot_read_or_continue_and_busy_returns_409(data, client):
    cid = data.store.create().id
    path = f"/api/knowledge-bases/{data.scope.knowledge_base_id}/conversations"
    payload = {"knowledge_base_id": str(data.scope.knowledge_base_id), "question": "q", "conversation_id": str(cid)}
    token = data.store.acquire(cid)
    assert client.post("/api/qa", json=payload).status_code == 409
    assert client.delete(f"{path}/{cid}").status_code == 409
    data.store.release(cid, token)
    client.app.dependency_overrides[get_current_principal] = lambda: Principal(data.other_user.id, data.principal.tenant_id, "bob")
    assert client.get(path).json()["items"] == []
    assert client.get(f"{path}/{cid}").status_code == 404
    assert client.post("/api/qa", json=payload).status_code == 404
    assert client.delete(f"{path}/{cid}").status_code == 404


def test_access_revocation_blocks_history_and_followup(data, client):
    cid = data.store.create().id
    data.session.execute(update(KnowledgeBase).where(KnowledgeBase.id == data.scope.knowledge_base_id).values(status="disabled"))
    data.session.commit()
    path = f"/api/knowledge-bases/{data.scope.knowledge_base_id}/conversations"
    assert client.get(path).status_code == 403
    assert client.get(f"{path}/{cid}").status_code == 403
    assert client.post(path).status_code == 403
    assert client.post("/api/qa", json={"knowledge_base_id": str(data.scope.knowledge_base_id),
        "question": "追问", "conversation_id": str(cid)}).status_code == 403
    assert client.app.state.answer_service.questions == []


def test_all_accessible_api_and_stateless_defaults_preserve_scope_isolation(data, client):
    response = client.post("/api/conversations")
    assert response.status_code == 201, response.text
    cid = response.json()["id"]
    assert response.json()["knowledge_base_id"] is None
    assert client.post("/api/qa", json={"question": "跨库问题"}).status_code == 200
    for question in ("首次跨库提问", "它的要求呢？"):
        response = client.post("/api/qa", json={"question": question, "conversation_id": cid})
        assert response.status_code == 200, response.text
    detail = client.get(f"/api/conversations/{cid}").json()
    assert detail["turn_count"] == 2
    assert len(detail["turns"]) == 2
    assert client.get("/api/conversations").json()["items"][0]["id"] == cid
    saved = data.session.scalars(select(ConversationTurn).where(ConversationTurn.conversation_id == UUID(cid))).all()
    assert set(saved[0].authorized_knowledge_base_ids) == {str(data.scope.knowledge_base_id), str(data.other_kb.id)}
    single_path = f"/api/knowledge-bases/{data.scope.knowledge_base_id}/conversations"
    assert client.get(f"{single_path}/{cid}").status_code == 404
    assert client.post("/api/qa", json={"knowledge_base_id": str(data.scope.knowledge_base_id),
        "conversation_id": cid, "question": "不应混用"}).status_code == 404
    single_cid = client.post(single_path).json()["id"]
    assert client.post("/api/qa", json={"question": "不应混用", "conversation_id": single_cid}).status_code == 404
    assert client.delete(f"/api/conversations/{cid}").status_code == 204
    assert data.session.scalars(select(ConversationTurn).where(ConversationTurn.conversation_id == UUID(cid))).all() == []


def test_all_scope_revocation_redacts_history_and_does_not_send_it_to_resolver(data, client):
    cid = client.post("/api/conversations").json()["id"]
    assert client.post("/api/qa", json={"question": "内部敏感标记", "conversation_id": cid}).status_code == 200
    data.session.execute(update(KnowledgeBase).where(KnowledgeBase.id == data.other_kb.id).values(status="disabled"))
    data.session.commit()
    detail = client.get(f"/api/conversations/{cid}")
    assert detail.status_code == 200
    assert "内部敏感标记" not in detail.text
    assert result().answer not in detail.text
    assert result().answer not in client.get("/api/conversations").text
    assert client.get("/api/conversations").json()["items"][0]["title"] == "历史权限已变更"
    response = client.post("/api/qa", json={"question": "重新提问", "conversation_id": cid})
    assert response.status_code == 200, response.text
    resolver = client.app.state.conversation_answer_service.resolver
    assert resolver.histories[-1] == []
    assert client.post("/api/qa", json={"question": "新的追问", "conversation_id": cid}).status_code == 200
    assert [turn.question for turn in resolver.histories[-1]] == ["重新提问"]


def test_all_scope_private_owner_and_no_access_checks(data, client):
    cid = client.post("/api/conversations").json()["id"]
    client.app.dependency_overrides[get_current_principal] = lambda: Principal(data.other_user.id, data.principal.tenant_id, "bob")
    assert client.get("/api/conversations").json()["items"] == []
    assert client.get(f"/api/conversations/{cid}").status_code == 404
    assert client.delete(f"/api/conversations/{cid}").status_code == 404
    assert client.post("/api/qa", json={"question": "q", "conversation_id": cid}).status_code == 404
    data.session.execute(update(KnowledgeBase).values(status="disabled"))
    data.session.commit()
    assert client.post("/api/qa", json={"question": "q"}).status_code == 403
    assert client.post("/api/conversations").status_code == 403
    assert client.app.state.answer_service.questions == []


def test_all_scope_nullable_fk_still_rejects_cross_tenant_turns(data):
    scope = RetrievalScope(data.scope.tenant_id, knowledge_base_ids={data.scope.knowledge_base_id})
    store = ConversationStore(data.session, data.principal, scope)
    cid = store.create().id
    data.session.add(ConversationTurn(tenant_id=data.other_tenant.id, knowledge_base_id=None,
        conversation_id=cid, turn_index=1, question="q", retrieval_question="q", result_json={}))
    with pytest.raises(IntegrityError):
        data.session.commit()
    data.session.rollback()


def test_migration_downgrade_refuses_to_delete_all_scope_history(data):
    import importlib
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    scope = RetrievalScope(data.scope.tenant_id, knowledge_base_ids={data.scope.knowledge_base_id})
    store = ConversationStore(data.session, data.principal, scope)
    cid = store.create().id
    migration = importlib.import_module("migrations.versions.b8d0f2a4c607_all_accessible_conversations")
    with Operations.context(MigrationContext.configure(data.session.connection())):
        with pytest.raises(RuntimeError, match="导出"):
            migration.downgrade()
    assert store.get(cid).id == cid
