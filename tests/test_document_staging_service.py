from hashlib import sha256
from io import BytesIO
from pathlib import Path
from uuid import uuid4

import pytest

from backend.app.document_staging_service import (
    DocumentStagingService,
    InvalidUpload,
    StagedDocument,
    UploadTooLarge,
)
from backend.app.security.retrieval_scope import RetrievalScope


@pytest.fixture
def scope() -> RetrievalScope:
    return RetrievalScope(
        tenant_id=uuid4(),
        knowledge_base_id=uuid4(),
    )


def test_stage_writes_candidate_without_replacing_target(
    tmp_path: Path,
    scope: RetrievalScope,
) -> None:
    storage_dir = tmp_path / "documents"
    target_path = (
        storage_dir
        / str(scope.tenant_id)
        / str(scope.knowledge_base_id)
        / "研发手册.txt"
    )
    target_path.parent.mkdir(parents=True)
    target_path.write_bytes(b"old-content")
    content = "新的企业知识库内容".encode("utf-8")
    service = DocumentStagingService(
        storage_dir=storage_dir,
        max_upload_bytes=1024,
    )

    staged = service.stage(
        file_name="研发手册.txt",
        source=BytesIO(content),
        scope=scope,
    )

    assert staged.target_path == target_path.resolve()
    assert staged.staged_path.parent.name == ".staging"
    assert staged.staged_path.suffix == ".txt"
    assert staged.staged_path.read_bytes() == content
    assert target_path.read_bytes() == b"old-content"
    assert staged.mime_type == "text/plain"
    assert staged.size_bytes == len(content)
    assert staged.content_hash == sha256(content).hexdigest()


def test_discard_is_idempotent_and_removes_empty_directory(
    tmp_path: Path,
    scope: RetrievalScope,
) -> None:
    service = DocumentStagingService(
        storage_dir=tmp_path / "documents",
        max_upload_bytes=1024,
    )
    staged = service.stage(
        file_name="制度.md",
        source=BytesIO(b"content"),
        scope=scope,
    )
    staging_directory = staged.staged_path.parent

    service.discard(staged)
    service.discard(staged)

    assert staged.staged_path.exists() is False
    assert staging_directory.exists() is False


def test_stage_cleans_partial_file_when_size_limit_is_exceeded(
    tmp_path: Path,
    scope: RetrievalScope,
) -> None:
    storage_dir = tmp_path / "documents"
    service = DocumentStagingService(
        storage_dir=storage_dir,
        max_upload_bytes=4,
    )

    with pytest.raises(
        UploadTooLarge,
        match="超过允许的最大大小",
    ):
        service.stage(
            file_name="过大文件.txt",
            source=BytesIO(b"12345"),
            scope=scope,
        )

    assert list(storage_dir.rglob("*")) == [
        storage_dir / str(scope.tenant_id),
        storage_dir
        / str(scope.tenant_id)
        / str(scope.knowledge_base_id),
    ]


def test_discard_rejects_file_outside_staging_directory(
    tmp_path: Path,
    scope: RetrievalScope,
) -> None:
    storage_dir = tmp_path / "documents"
    protected_file = tmp_path / "do-not-delete.txt"
    protected_file.write_bytes(b"protected")
    service = DocumentStagingService(
        storage_dir=storage_dir,
        max_upload_bytes=1024,
    )
    forged = StagedDocument(
        file_name="do-not-delete.txt",
        staged_path=protected_file,
        target_path=protected_file,
        mime_type="text/plain",
        content_hash=sha256(b"protected").hexdigest(),
        size_bytes=9,
    )

    with pytest.raises(
        InvalidUpload,
        match="不在受管存储目录中",
    ):
        service.discard(forged)

    assert protected_file.read_bytes() == b"protected"
