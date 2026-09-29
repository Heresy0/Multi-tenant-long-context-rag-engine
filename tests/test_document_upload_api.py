from collections.abc import Iterator
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, event, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

import backend.app.api.knowledge_bases as knowledge_bases_api
from backend.app.api.knowledge_bases import (
    router as knowledge_base_router,
)
from backend.app.db.base import Base
from backend.app.db.models import (
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
from backend.app.security.retrieval_scope import RetrievalScope


@dataclass(frozen=True, slots=True)
class UploadApiContext:
    app: FastAPI
    session_factory: object
    storage_dir: Path
    tenant_id: UUID
    knowledge_base_id: UUID
    editor: Principal
    viewer: Principal
    indexing_calls: list[tuple[Path, RetrievalScope, UUID]]
    indexing_behavior: dict[str, object]


@pytest.fixture
def api_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[UploadApiContext]:
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
    knowledge_base_id = uuid4()

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
                id=knowledge_base_id,
                tenant_id=tenant_id,
                name="技术部知识库",
                visibility="restricted",
            ),
        ])
        session.flush()
        session.add_all([
            KnowledgeBaseUserGrant(
                tenant_id=tenant_id,
                knowledge_base_id=knowledge_base_id,
                user_id=editor_id,
                permission="editor",
            ),
            KnowledgeBaseUserGrant(
                tenant_id=tenant_id,
                knowledge_base_id=knowledge_base_id,
                user_id=viewer_id,
                permission="viewer",
            ),
        ])
        session.commit()

    indexing_calls: list[
        tuple[Path, RetrievalScope, UUID]
    ] = []
    indexing_behavior: dict[str, object] = {
        "error": None,
        "chunk_count": 2,
    }

    class FakePgVectorIndexingService:
        def __init__(
            self,
            *,
            session: Session,
            settings,
        ) -> None:
            del settings
            self._session = session

        def index_file(
            self,
            *,
            file_path: Path,
            scope: RetrievalScope,
            created_by_user_id: UUID,
        ) -> int:
            indexing_calls.append((
                file_path,
                scope,
                created_by_user_id,
            ))

            error = indexing_behavior["error"]
            if isinstance(error, Exception):
                raise error

            chunk_count = int(
                indexing_behavior["chunk_count"]
            )
            document = KnowledgeDocument(
                tenant_id=scope.tenant_id,
                knowledge_base_id=(
                    scope.knowledge_base_id
                ),
                created_by_user_id=created_by_user_id,
                source_id=sha256(
                    file_path.as_uri().encode("utf-8")
                ).hexdigest(),
                file_name=file_path.name,
                storage_uri=file_path.as_uri(),
                mime_type="text/plain",
                content_hash=sha256(
                    file_path.read_bytes()
                ).hexdigest(),
                status="ready",
                version=1,
                metadata_json={
                    "chunk_count": chunk_count,
                },
            )
            self._session.add(document)
            self._session.commit()
            return chunk_count

    monkeypatch.setattr(
        knowledge_bases_api,
        "PgVectorIndexingService",
        FakePgVectorIndexingService,
    )

    storage_dir = tmp_path / "documents"
    app = FastAPI()
    app.state.database_session_factory = session_factory
    app.state.settings = SimpleNamespace(
        document_storage_dir=storage_dir,
        max_upload_bytes=1024,
    )
    app.include_router(knowledge_base_router)

    try:
        yield UploadApiContext(
            app=app,
            session_factory=session_factory,
            storage_dir=storage_dir,
            tenant_id=tenant_id,
            knowledge_base_id=knowledge_base_id,
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
            indexing_calls=indexing_calls,
            indexing_behavior=indexing_behavior,
        )

    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


def _set_principal(
    context: UploadApiContext,
    principal: Principal,
) -> None:
    context.app.dependency_overrides[
        get_current_principal
    ] = lambda: principal


def _upload_url(context: UploadApiContext) -> str:
    return (
        "/api/knowledge-bases/"
        f"{context.knowledge_base_id}/documents"
    )


def _stored_file(
    context: UploadApiContext,
    file_name: str,
) -> Path:
    return (
        context.storage_dir
        / str(context.tenant_id)
        / str(context.knowledge_base_id)
        / file_name
    )


def test_editor_can_upload_document(
    api_context: UploadApiContext,
) -> None:
    _set_principal(api_context, api_context.editor)

    response = TestClient(api_context.app).post(
        _upload_url(api_context),
        files={
            "file": (
                "研发手册.txt",
                "企业知识库内容".encode("utf-8"),
                "text/plain",
            ),
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["document"]["file_name"] == (
        "研发手册.txt"
    )
    assert payload["document"]["status"] == "ready"
    assert payload["document"]["chunk_count"] == 2
    assert payload["indexed_chunk_count"] == 2
    assert payload["skipped"] is False
    assert "storage_uri" not in payload["document"]

    stored_file = _stored_file(
        api_context,
        "研发手册.txt",
    )
    assert stored_file.read_text(encoding="utf-8") == (
        "企业知识库内容"
    )
    assert api_context.indexing_calls == [(
        stored_file.resolve(),
        RetrievalScope(
            tenant_id=api_context.tenant_id,
            knowledge_base_id=(
                api_context.knowledge_base_id
            ),
        ),
        api_context.editor.user_id,
    )]

    with api_context.session_factory() as session:
        document_count = session.scalar(
            select(func.count()).select_from(
                KnowledgeDocument
            )
        )

    assert document_count == 1


def test_viewer_cannot_upload_document(
    api_context: UploadApiContext,
) -> None:
    _set_principal(api_context, api_context.viewer)

    response = TestClient(api_context.app).post(
        _upload_url(api_context),
        files={
            "file": (
                "研发手册.txt",
                b"content",
                "text/plain",
            ),
        },
    )

    assert response.status_code == 403
    assert response.json() == {
        "detail": "没有权限访问该知识库。",
    }
    assert api_context.indexing_calls == []
    assert api_context.storage_dir.exists() is False


def test_upload_rejects_unsupported_file_type(
    api_context: UploadApiContext,
) -> None:
    _set_principal(api_context, api_context.editor)

    response = TestClient(api_context.app).post(
        _upload_url(api_context),
        files={
            "file": (
                "program.exe",
                b"content",
                "application/octet-stream",
            ),
        },
    )

    assert response.status_code == 400
    assert response.json() == {
        "detail": "暂不支持该文件类型：.exe",
    }
    assert api_context.indexing_calls == []
    assert api_context.storage_dir.exists() is False


def test_upload_rejects_file_over_size_limit(
    api_context: UploadApiContext,
) -> None:
    _set_principal(api_context, api_context.editor)
    api_context.app.state.settings.max_upload_bytes = 4

    response = TestClient(api_context.app).post(
        _upload_url(api_context),
        files={
            "file": (
                "oversized.txt",
                b"12345",
                "text/plain",
            ),
        },
    )

    assert response.status_code == 413
    assert response.json() == {
        "detail": "上传文件超过允许的最大大小",
    }
    assert api_context.indexing_calls == []
    assert _stored_file(
        api_context,
        "oversized.txt",
    ).exists() is False


def test_upload_removes_file_when_indexing_fails(
    api_context: UploadApiContext,
) -> None:
    _set_principal(api_context, api_context.editor)
    api_context.indexing_behavior["error"] = (
        RuntimeError("embedding service unavailable")
    )

    response = TestClient(
        api_context.app,
        raise_server_exceptions=False,
    ).post(
        _upload_url(api_context),
        files={
            "file": (
                "研发手册.txt",
                b"content",
                "text/plain",
            ),
        },
    )

    assert response.status_code == 500
    assert response.json() == {
        "detail": "文档入库失败，请稍后重试。",
    }
    assert len(api_context.indexing_calls) == 1
    assert _stored_file(
        api_context,
        "研发手册.txt",
    ).exists() is False
