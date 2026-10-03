"""Run with the same dedicated, migrated BM25 test database used by CI."""
import importlib

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from uuid import uuid4

from backend.app.db.conversation_models import ConversationTurn
from backend.app.db.models import User
from backend.app.knowledge.answer_models import AnswerResult
from backend.app.knowledge.conversations import ConversationStore
from backend.app.security.principal import Principal
from backend.app.security.retrieval_scope import RetrievalScope
from tests.test_pg_bm25_integration import bm25_session, _scope  # noqa: F401

pytestmark = pytest.mark.integration


def test_postgres_migration_preserves_legacy_history_and_all_scope_tenant_fk(bm25_session):
    scope, tenant = _scope(bm25_session)
    user = User(tenant_id=tenant.id, external_subject=uuid4().hex, name="Migration test")
    bm25_session.add(user)
    bm25_session.flush()
    principal = Principal(user.id, tenant.id, user.external_subject)
    store = ConversationStore(bm25_session, principal, scope)
    cid = store.create().id
    token = store.acquire(cid)
    store.append(cid, token, "old question", "old question", AnswerResult(answer="old answer", answerable=False, citations=[]))
    migration = importlib.import_module("migrations.versions.b8d0f2a4c607_all_accessible_conversations")
    connection = bm25_session.connection()
    with Operations.context(MigrationContext.configure(connection)):
        migration.downgrade()
        migration.upgrade()
    bm25_session.expire_all()
    assert store.get(cid).knowledge_base_id == scope.knowledge_base_id
    history = store.turns(cid)
    assert history[0].question == "old question"
    assert history[0].result_json["answer"] == "old answer"
    assert history[0].authorized_knowledge_base_ids == []
    combined = RetrievalScope(tenant.id, knowledge_base_ids={scope.knowledge_base_id})
    all_store = ConversationStore(bm25_session, principal, combined)
    all_cid = all_store.create().id
    with pytest.raises(IntegrityError):
        with bm25_session.begin_nested():
            bm25_session.add(ConversationTurn(tenant_id=uuid4(), knowledge_base_id=None,
                conversation_id=all_cid, turn_index=1, question="invalid", retrieval_question="invalid", result_json={}))
            bm25_session.flush()
    with Operations.context(MigrationContext.configure(bm25_session.connection())):
        with pytest.raises(RuntimeError, match="导出"):
            migration.downgrade()
    assert all_store.get(all_cid).knowledge_base_id is None
    assert bm25_session.scalar(select(ConversationTurn.question).where(ConversationTurn.conversation_id == cid)) == "old question"
