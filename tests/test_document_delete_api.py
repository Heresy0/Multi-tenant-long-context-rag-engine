from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, event, func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

import backend.app.api.knowledge_bases as knowledge_bases_api
from backend.app.api.knowledge_bases import (
    router as knowledge_base_router,
)
from backend.app.db.base import Base
from backend.app.db.models import (
    EMBEDDING_DIMENSION,
    DocumentChunk,
    KnowledgeBase,
    KnowledgeBaseUserGrant,
    KnowledgeDocument,
    Tenant,
    User,
)
from backend.app.db.session import create_session_factory
from backend.app.security.dependencies import (
    get_current_principal,
)
from backend.app.security.principal import Principal


@dataclass(frozen=True, slots=True)
class DeleteApiContext:
    app: FastAPI
    session_factory: sessionmaker[Session]
    storage_dir: Path
    primary_kb_id: UUID
    other_kb_id: UUID
    primary_document_id: UUID
    other_document_id: UUID
    primary_file: Path
    other_file: Path
    editor: Principal
    viewer: Principal


@pytest.fixture
def api_context(
    tmp_path: Path,
) -> Iterator[DeleteApiContext]:
    engine: Engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={
            "check_same_thread": False,
        },
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(
        dbapi_connection,
        _connection_record,
    ) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys = ON")
        cursor.close()

    Base.metadata.create_all(engine)
    session_factory = create_session_factory(engine)

    tenant_id = uuid4()
    editor_id = uuid4()
    viewer_id = uuid4()
    primary_kb_id = uuid4()
    other_kb_id = uuid4()
    primary_document_id = uuid4()
    other_document_id = uuid4()

    storage_dir = tmp_path / "documents"
    primary_file = (
        storage_dir
        / str(tenant_id)
        / str(primary_kb_id)
        / "primary.txt"
    )
    other_file = (
        storage_dir
        / str(tenant_id)
        / str(other_kb_id)
        / "other.txt"
    )
    primary_file.parent.mkdir(parents=True)
    other_file.parent.mkdir(parents=True)
    primary_file.write_text(
        "primary document",
        encoding="utf-8",
    )
    other_file.write_text(
        "other document",
        encoding="utf-8",
    )

    with session_factory() as session:
        session.add(
            Tenant(
                id=tenant_id,
                name="测试企业",
            )
        )
        session.flush()
        session.add_all([
            User(
                id=editor_id,
                tenant_id=tenant_id,
                external_subject="editor",
                name="编辑者",
            ),
            User(
                id=viewer_id,
                tenant_id=tenant_id,
                external_subject="viewer",
                name="只读用户",
            ),
            KnowledgeBase(
                id=primary_kb_id,
                tenant_id=tenant_id,
                name="技术部知识库",
                visibility="restricted",
            ),
            KnowledgeBase(
                id=other_kb_id,
                tenant_id=tenant_id,
                name="其他知识库",
                visibility="restricted",
            ),
        ])
        session.flush()
        session.add_all([
            KnowledgeBaseUserGrant(
                tenant_id=tenant_id,
                knowledge_base_id=primary_kb_id,
                user_id=editor_id,
                permission="editor",
            ),
            KnowledgeBaseUserGrant(
                tenant_id=tenant_id,
                knowledge_base_id=primary_kb_id,
                user_id=viewer_id,
                permission="viewer",
            ),
        ])
        session.add_all([
            KnowledgeDocument(
                id=primary_document_id,
                tenant_id=tenant_id,
                knowledge_base_id=primary_kb_id,
                created_by_user_id=editor_id,
                source_id="primary-document",
                file_name="primary.txt",
                storage_uri=primary_file.as_uri(),
                mime_type="text/plain",
                content_hash="a" * 64,
                status="ready",
                version=1,
                metadata_json={
                    "chunk_count": 1,
                },
            ),
            KnowledgeDocument(
                id=other_document_id,
                tenant_id=tenant_id,
                knowledge_base_id=other_kb_id,
                created_by_user_id=editor_id,
                source_id="other-document",
                file_name="other.txt",
                storage_uri=other_file.as_uri(),
                mime_type="text/plain",
                content_hash="b" * 64,
                status="ready",
                version=1,
                metadata_json={
                    "chunk_count": 0,
                },
            ),
        ])
        session.flush()
        session.add(
            DocumentChunk(
                id="primary-chunk",
                tenant_id=tenant_id,
                knowledge_base_id=primary_kb_id,
                document_id=primary_document_id,
                chunk_index=0,
                content="primary document",
                content_hash="c" * 64,
                parent_id=None,
                chunking_version="test-v1",
                embedding_model="test-embedding",
                embedding=[0.0] * EMBEDDING_DIMENSION,
                metadata_json={},
            )
        )
        session.commit()

    app = FastAPI()
    app.state.database_session_factory = session_factory
    app.state.settings = SimpleNamespace(
        document_storage_dir=storage_dir,
    )
    app.include_router(knowledge_base_router)

    try:
        yield DeleteApiContext(
            app=app,
            session_factory=session_factory,
            storage_dir=storage_dir,
            primary_kb_id=primary_kb_id,
            other_kb_id=other_kb_id,
            primary_document_id=primary_document_id,
            other_document_id=other_document_id,
            primary_file=primary_file,
            other_file=other_file,
            editor=Principal(
                user_id=editor_id,
                tenant_id=tenant_id,
                external_subject="editor",
            ),
            viewer=Principal(
                user_id=viewer_id,
                tenant_id=tenant_id,
                external_subject="viewer",
            ),
        )

    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


def _set_principal(
    context: DeleteApiContext,
    principal: Principal,
) -> None:
    context.app.dependency_overrides[
        get_current_principal
    ] = lambda: principal


def _delete_url(
    context: DeleteApiContext,
    *,
    knowledge_base_id: UUID,
    document_id: UUID,
) -> str:
    return (
        "/api/knowledge-bases/"
        f"{knowledge_base_id}/documents/{document_id}"
    )


def _row_count(
    context: DeleteApiContext,
    model,
    condition,
) -> int:
    with context.session_factory() as session:
        return session.scalar(
            select(func.count())
            .select_from(model)
            .where(condition)
        ) or 0


def test_editor_can_delete_document(
    api_context: DeleteApiContext,
) -> None:
    _set_principal(api_context, api_context.editor)

    response = TestClient(api_context.app).delete(
        _delete_url(
            api_context,
            knowledge_base_id=api_context.primary_kb_id,
            document_id=api_context.primary_document_id,
        )
    )

    assert response.status_code == 204
    assert response.content == b""
    assert api_context.primary_file.exists() is False
    assert _row_count(
        api_context,
        KnowledgeDocument,
        KnowledgeDocument.id
        == api_context.primary_document_id,
    ) == 0
    assert _row_count(
        api_context,
        DocumentChunk,
        DocumentChunk.document_id
        == api_context.primary_document_id,
    ) == 0
    assert api_context.other_file.is_file()


def test_viewer_cannot_delete_document(
    api_context: DeleteApiContext,
) -> None:
    _set_principal(api_context, api_context.viewer)

    response = TestClient(api_context.app).delete(
        _delete_url(
            api_context,
            knowledge_base_id=api_context.primary_kb_id,
            document_id=api_context.primary_document_id,
        )
    )

    assert response.status_code == 403
    assert response.json() == {
        "detail": "没有权限访问该知识库。",
    }
    assert api_context.primary_file.is_file()
    assert _row_count(
        api_context,
        KnowledgeDocument,
        KnowledgeDocument.id
        == api_context.primary_document_id,
    ) == 1


def test_document_from_another_knowledge_base_returns_404(
    api_context: DeleteApiContext,
) -> None:
    _set_principal(api_context, api_context.editor)

    response = TestClient(api_context.app).delete(
        _delete_url(
            api_context,
            knowledge_base_id=api_context.primary_kb_id,
            document_id=api_context.other_document_id,
        )
    )

    assert response.status_code == 404
    assert response.json() == {
        "detail": "文档不存在。",
    }
    assert api_context.other_file.is_file()
    assert _row_count(
        api_context,
        KnowledgeDocument,
        KnowledgeDocument.id
        == api_context.other_document_id,
    ) == 1


def test_delete_returns_500_when_service_fails(
    api_context: DeleteApiContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_principal(api_context, api_context.editor)

    class FailingDeletionService:
        def __init__(
            self,
            *,
            session: Session,
            storage_dir: str | Path,
        ) -> None:
            del session, storage_dir

        def delete(self, **_kwargs) -> None:
            raise RuntimeError("database unavailable")

    monkeypatch.setattr(
        knowledge_bases_api,
        "DocumentDeletionService",
        FailingDeletionService,
    )

    response = TestClient(
        api_context.app,
        raise_server_exceptions=False,
    ).delete(
        _delete_url(
            api_context,
            knowledge_base_id=api_context.primary_kb_id,
            document_id=api_context.primary_document_id,
        )
    )

    assert response.status_code == 500
    assert response.json() == {
        "detail": "文档删除失败，请稍后重试。",
    }
    assert api_context.primary_file.is_file()
    assert _row_count(
        api_context,
        KnowledgeDocument,
        KnowledgeDocument.id
        == api_context.primary_document_id,
    ) == 1
