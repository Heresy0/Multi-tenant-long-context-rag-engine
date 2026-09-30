from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, event, func, select
from sqlalchemy.pool import StaticPool

from backend.app.api.knowledge_bases import (
    router as knowledge_base_router,
)
from backend.app.db.base import Base
from backend.app.db.models import (
    DocumentIndexingJob,
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
class UploadApiContext:
    app: FastAPI
    session_factory: object
    storage_dir: Path
    tenant_id: UUID
    knowledge_base_id: UUID
    editor: Principal
    viewer: Principal


@pytest.fixture
def api_context(
    tmp_path: Path,
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


def _job_url(
    context: UploadApiContext,
    job_id: str,
) -> str:
    return (
        "/api/knowledge-bases/"
        f"{context.knowledge_base_id}/"
        f"indexing-jobs/{job_id}"
    )


def _job_list_url(context: UploadApiContext) -> str:
    return (
        "/api/knowledge-bases/"
        f"{context.knowledge_base_id}/indexing-jobs"
    )


def _job_retry_url(
    context: UploadApiContext,
    job_id: str,
) -> str:
    return f"{_job_url(context, job_id)}/retry"


def _staged_files(
    context: UploadApiContext,
) -> list[Path]:
    directory = (
        context.storage_dir
        / str(context.tenant_id)
        / str(context.knowledge_base_id)
        / ".staging"
    )

    if not directory.exists():
        return []

    return [
        path
        for path in directory.iterdir()
        if path.is_file()
    ]


def test_editor_upload_is_accepted_and_queued(
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

    assert response.status_code == 202
    payload = response.json()
    assert payload["document"]["file_name"] == (
        "研发手册.txt"
    )
    assert payload["document"]["status"] == "pending"
    assert payload["document"]["version"] == 1
    assert payload["document"]["chunk_count"] == 0
    assert "storage_uri" not in payload["document"]
    assert payload["indexing_job"]["status"] == "queued"
    assert payload["indexing_job"]["target_version"] == 1
    assert payload["indexing_job"]["attempt_count"] == 0
    assert payload["indexing_job"]["max_attempts"] == 3
    assert "staged_storage_uri" not in payload["indexing_job"]

    staged_files = _staged_files(api_context)
    assert len(staged_files) == 1
    assert staged_files[0].read_text(encoding="utf-8") == (
        "企业知识库内容"
    )
    assert (
        api_context.storage_dir
        / str(api_context.tenant_id)
        / str(api_context.knowledge_base_id)
        / "研发手册.txt"
    ).exists() is False

    with api_context.session_factory() as session:
        document_count = session.scalar(
            select(func.count()).select_from(
                KnowledgeDocument
            )
        )
        job = session.scalar(
            select(DocumentIndexingJob)
        )

    assert document_count == 1
    assert job is not None
    assert str(job.id) == payload["indexing_job"]["id"]


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
    assert _staged_files(api_context) == []


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
    assert _staged_files(api_context) == []


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
    assert _staged_files(api_context) == []


def test_second_active_upload_returns_conflict(
    api_context: UploadApiContext,
) -> None:
    _set_principal(api_context, api_context.editor)
    client = TestClient(api_context.app)

    first = client.post(
        _upload_url(api_context),
        files={
            "file": (
                "并发上传.txt",
                b"first",
                "text/plain",
            ),
        },
    )
    second = client.post(
        _upload_url(api_context),
        files={
            "file": (
                "并发上传.txt",
                b"second",
                "text/plain",
            ),
        },
    )

    assert first.status_code == 202
    assert second.status_code == 409
    assert second.json() == {
        "detail": "该文档已经存在活动索引任务。",
    }
    assert len(_staged_files(api_context)) == 1

    with api_context.session_factory() as session:
        document_count = session.scalar(
            select(func.count()).select_from(
                KnowledgeDocument
            )
        )
        job_count = session.scalar(
            select(func.count()).select_from(
                DocumentIndexingJob
            )
        )

    assert document_count == 1
    assert job_count == 1


def test_editor_can_query_job_but_viewer_cannot(
    api_context: UploadApiContext,
) -> None:
    client = TestClient(api_context.app)
    _set_principal(api_context, api_context.editor)
    upload_response = client.post(
        _upload_url(api_context),
        files={
            "file": (
                "任务状态.txt",
                b"content",
                "text/plain",
            ),
        },
    )
    job_id = upload_response.json()[
        "indexing_job"
    ]["id"]

    response = client.get(
        _job_url(api_context, job_id)
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["id"] == job_id
    assert payload["status"] == "queued"
    assert payload["indexed_chunk_count"] is None
    assert payload["last_error"] is None
    assert payload["started_at"] is None
    assert payload["finished_at"] is None

    _set_principal(api_context, api_context.viewer)
    forbidden = client.get(
        _job_url(api_context, job_id)
    )

    assert forbidden.status_code == 403

    _set_principal(api_context, api_context.editor)
    missing = client.get(
        _job_url(api_context, str(uuid4()))
    )

    assert missing.status_code == 404
    assert missing.json() == {
        "detail": "索引任务不存在。",
    }


def test_editor_can_list_and_filter_indexing_jobs(
    api_context: UploadApiContext,
) -> None:
    client = TestClient(api_context.app)
    _set_principal(api_context, api_context.editor)
    upload = client.post(
        _upload_url(api_context),
        files={
            "file": (
                "任务列表.txt",
                b"content",
                "text/plain",
            ),
        },
    )
    job_id = upload.json()["indexing_job"]["id"]

    response = client.get(
        _job_list_url(api_context),
        params={"status": "queued", "limit": 10},
    )

    assert response.status_code == 200
    assert [
        item["id"]
        for item in response.json()["items"]
    ] == [job_id]

    invalid = client.get(
        _job_list_url(api_context),
        params={"status": "cancelled"},
    )
    assert invalid.status_code == 422

    _set_principal(api_context, api_context.viewer)
    forbidden = client.get(
        _job_list_url(api_context)
    )
    assert forbidden.status_code == 403


def test_editor_can_retry_failed_job_with_retained_file(
    api_context: UploadApiContext,
) -> None:
    client = TestClient(api_context.app)
    _set_principal(api_context, api_context.editor)
    upload = client.post(
        _upload_url(api_context),
        files={
            "file": (
                "人工重试.txt",
                b"retry-content",
                "text/plain",
            ),
        },
    )
    payload = upload.json()
    job_id = payload["indexing_job"]["id"]
    document_id = payload["document"]["id"]

    with api_context.session_factory() as session:
        job = session.get(
            DocumentIndexingJob,
            UUID(job_id),
        )
        document = session.get(
            KnowledgeDocument,
            UUID(document_id),
        )
        assert job is not None
        assert document is not None
        job.status = "failed"
        job.attempt_count = job.max_attempts
        job.last_error = "model unavailable"
        document.status = "failed"
        document.last_error = "model unavailable"
        session.commit()

    response = client.post(
        _job_retry_url(api_context, job_id)
    )

    assert response.status_code == 200
    retried = response.json()
    assert retried["status"] == "queued"
    assert retried["attempt_count"] == 0
    assert retried["last_error"] is None

    with api_context.session_factory() as session:
        document = session.get(
            KnowledgeDocument,
            UUID(document_id),
        )
        assert document is not None
        assert document.status == "pending"
        assert document.last_error is None


def test_retry_rejects_missing_file_and_viewer(
    api_context: UploadApiContext,
) -> None:
    client = TestClient(api_context.app)
    _set_principal(api_context, api_context.editor)
    upload = client.post(
        _upload_url(api_context),
        files={
            "file": (
                "候选丢失.txt",
                b"content",
                "text/plain",
            ),
        },
    )
    job_id = upload.json()["indexing_job"]["id"]

    with api_context.session_factory() as session:
        job = session.get(
            DocumentIndexingJob,
            UUID(job_id),
        )
        assert job is not None
        job.status = "failed"
        job.attempt_count = job.max_attempts
        session.commit()

    _staged_files(api_context)[0].unlink()
    missing = client.post(
        _job_retry_url(api_context, job_id)
    )
    assert missing.status_code == 409
    assert "重新上传" in missing.json()["detail"]

    _set_principal(api_context, api_context.viewer)
    forbidden = client.post(
        _job_retry_url(api_context, job_id)
    )
    assert forbidden.status_code == 403
