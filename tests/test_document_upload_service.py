from collections.abc import Iterator
from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, create_engine, event, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from backend.app.db.base import Base
from backend.app.db.models import (
    KnowledgeBase,
    KnowledgeDocument,
    Tenant,
    User,
)
from backend.app.document_upload_service import (
    DocumentUploadService,
    InvalidUpload,
    UnsupportedUploadType,
    UploadTooLarge,
)
from backend.app.security.retrieval_scope import RetrievalScope


@dataclass(frozen=True, slots=True)
class ScopeData:
    scope: RetrievalScope
    user_id: UUID


@dataclass(frozen=True, slots=True)
class IndexCall:
    file_path: Path
    scope: RetrievalScope
    created_by_user_id: UUID


class FakeIndexingService:
    """用数据库记录模拟真实索引服务的最终结果。"""

    def __init__(
        self,
        session: Session,
        *,
        results: list[int] | None = None,
        error: Exception | None = None,
    ) -> None:
        self._session = session
        self._results = results or [3]
        self._error = error
        self.calls: list[IndexCall] = []

    def index_file(
        self,
        *,
        file_path: Path,
        scope: RetrievalScope,
        created_by_user_id: UUID,
    ) -> int:
        self.calls.append(
            IndexCall(
                file_path=file_path,
                scope=scope,
                created_by_user_id=created_by_user_id,
            )
        )

        if self._error is not None:
            raise self._error

        result_index = min(
            len(self.calls) - 1,
            len(self._results) - 1,
        )
        indexed_chunk_count = self._results[result_index]

        document = self._session.scalar(
            select(KnowledgeDocument).where(
                KnowledgeDocument.tenant_id
                == scope.tenant_id,
                KnowledgeDocument.knowledge_base_id
                == scope.knowledge_base_id,
                KnowledgeDocument.storage_uri
                == file_path.as_uri(),
            )
        )

        if document is None:
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
                    "chunk_count": indexed_chunk_count,
                },
            )
            self._session.add(document)
        elif indexed_chunk_count > 0:
            document.content_hash = sha256(
                file_path.read_bytes()
            ).hexdigest()
            document.version += 1
            document.metadata_json = {
                "chunk_count": indexed_chunk_count,
            }

        self._session.commit()
        return indexed_chunk_count


@pytest.fixture
def session() -> Iterator[Session]:
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

    with Session(engine) as database_session:
        yield database_session

    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture
def scope_data(session: Session) -> ScopeData:
    tenant_id = uuid4()
    user_id = uuid4()
    knowledge_base_id = uuid4()

    session.add(
        Tenant(
            id=tenant_id,
            name="星海科技有限公司",
        )
    )
    session.flush()
    session.add_all([
        User(
            id=user_id,
            tenant_id=tenant_id,
            external_subject="keycloak-alice",
            name="Alice",
        ),
        KnowledgeBase(
            id=knowledge_base_id,
            tenant_id=tenant_id,
            name="技术部知识库",
            visibility="restricted",
        ),
    ])
    session.commit()

    return ScopeData(
        scope=RetrievalScope(
            tenant_id=tenant_id,
            knowledge_base_id=knowledge_base_id,
        ),
        user_id=user_id,
    )


def _target_path(
    storage_dir: Path,
    scope: RetrievalScope,
    file_name: str,
) -> Path:
    return (
        storage_dir
        / str(scope.tenant_id)
        / str(scope.knowledge_base_id)
        / file_name
    )


def _temporary_files(directory: Path) -> list[Path]:
    if not directory.exists():
        return []

    return [
        path
        for path in directory.iterdir()
        if path.name.startswith((".upload-", ".backup-"))
    ]


def test_upload_saves_file_and_returns_document(
    session: Session,
    scope_data: ScopeData,
    tmp_path: Path,
) -> None:
    storage_dir = tmp_path / "uploads"
    indexing_service = FakeIndexingService(
        session,
        results=[3],
    )
    service = DocumentUploadService(
        session=session,
        indexing_service=indexing_service,
        storage_dir=storage_dir,
        max_upload_bytes=1024,
    )

    result = service.upload(
        file_name="研发规范.txt",
        source=BytesIO("企业知识库内容".encode("utf-8")),
        scope=scope_data.scope,
        created_by_user_id=scope_data.user_id,
    )

    target_path = _target_path(
        storage_dir,
        scope_data.scope,
        "研发规范.txt",
    )
    assert target_path.read_text(encoding="utf-8") == (
        "企业知识库内容"
    )
    assert result.document.file_name == "研发规范.txt"
    assert result.document.storage_uri == target_path.as_uri()
    assert result.indexed_chunk_count == 3
    assert result.skipped is False
    assert indexing_service.calls == [
        IndexCall(
            file_path=target_path.resolve(),
            scope=scope_data.scope,
            created_by_user_id=scope_data.user_id,
        )
    ]
    assert _temporary_files(target_path.parent) == []


def test_upload_reports_unchanged_file_as_skipped(
    session: Session,
    scope_data: ScopeData,
    tmp_path: Path,
) -> None:
    storage_dir = tmp_path / "uploads"
    indexing_service = FakeIndexingService(
        session,
        results=[2, 0],
    )
    service = DocumentUploadService(
        session=session,
        indexing_service=indexing_service,
        storage_dir=storage_dir,
        max_upload_bytes=1024,
    )
    content = "没有发生变化的内容".encode("utf-8")

    first_result = service.upload(
        file_name="手册.md",
        source=BytesIO(content),
        scope=scope_data.scope,
        created_by_user_id=scope_data.user_id,
    )
    second_result = service.upload(
        file_name="手册.md",
        source=BytesIO(content),
        scope=scope_data.scope,
        created_by_user_id=scope_data.user_id,
    )

    assert first_result.skipped is False
    assert second_result.indexed_chunk_count == 0
    assert second_result.skipped is True
    assert second_result.document.id == first_result.document.id
    assert second_result.document.version == 1
    assert len(indexing_service.calls) == 2


def test_upload_rejects_unsupported_file_before_writing(
    session: Session,
    scope_data: ScopeData,
    tmp_path: Path,
) -> None:
    storage_dir = tmp_path / "uploads"
    indexing_service = FakeIndexingService(session)
    service = DocumentUploadService(
        session=session,
        indexing_service=indexing_service,
        storage_dir=storage_dir,
        max_upload_bytes=1024,
    )

    with pytest.raises(
        UnsupportedUploadType,
        match="暂不支持该文件类型",
    ):
        service.upload(
            file_name="恶意程序.exe",
            source=BytesIO(b"not-an-executable"),
            scope=scope_data.scope,
            created_by_user_id=scope_data.user_id,
        )

    assert storage_dir.exists() is False
    assert indexing_service.calls == []


def test_upload_rejects_file_over_size_limit_and_cleans_up(
    session: Session,
    scope_data: ScopeData,
    tmp_path: Path,
) -> None:
    storage_dir = tmp_path / "uploads"
    indexing_service = FakeIndexingService(session)
    service = DocumentUploadService(
        session=session,
        indexing_service=indexing_service,
        storage_dir=storage_dir,
        max_upload_bytes=4,
    )

    with pytest.raises(
        UploadTooLarge,
        match="超过允许的最大大小",
    ):
        service.upload(
            file_name="过大文件.txt",
            source=BytesIO(b"12345"),
            scope=scope_data.scope,
            created_by_user_id=scope_data.user_id,
        )

    target_path = _target_path(
        storage_dir,
        scope_data.scope,
        "过大文件.txt",
    )
    assert target_path.exists() is False
    assert _temporary_files(target_path.parent) == []
    assert indexing_service.calls == []


def test_upload_rejects_empty_file_and_cleans_up(
    session: Session,
    scope_data: ScopeData,
    tmp_path: Path,
) -> None:
    storage_dir = tmp_path / "uploads"
    indexing_service = FakeIndexingService(session)
    service = DocumentUploadService(
        session=session,
        indexing_service=indexing_service,
        storage_dir=storage_dir,
        max_upload_bytes=1024,
    )

    with pytest.raises(
        InvalidUpload,
        match="上传文件不能为空",
    ):
        service.upload(
            file_name="空文件.txt",
            source=BytesIO(b""),
            scope=scope_data.scope,
            created_by_user_id=scope_data.user_id,
        )

    target_path = _target_path(
        storage_dir,
        scope_data.scope,
        "空文件.txt",
    )
    assert target_path.exists() is False
    assert _temporary_files(target_path.parent) == []
    assert indexing_service.calls == []


def test_index_failure_restores_previous_file(
    session: Session,
    scope_data: ScopeData,
    tmp_path: Path,
) -> None:
    storage_dir = tmp_path / "uploads"
    target_path = _target_path(
        storage_dir,
        scope_data.scope,
        "生产手册.txt",
    )
    target_path.parent.mkdir(parents=True)
    target_path.write_bytes(b"old-content")

    indexing_service = FakeIndexingService(
        session,
        error=RuntimeError("embedding service unavailable"),
    )
    service = DocumentUploadService(
        session=session,
        indexing_service=indexing_service,
        storage_dir=storage_dir,
        max_upload_bytes=1024,
    )

    with pytest.raises(
        RuntimeError,
        match="embedding service unavailable",
    ):
        service.upload(
            file_name="生产手册.txt",
            source=BytesIO(b"new-content"),
            scope=scope_data.scope,
            created_by_user_id=scope_data.user_id,
        )

    assert target_path.read_bytes() == b"old-content"
    assert _temporary_files(target_path.parent) == []
    assert len(indexing_service.calls) == 1
