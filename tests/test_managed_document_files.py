from pathlib import Path
from uuid import uuid4

import pytest

from backend.app.managed_document_files import (
    ManagedDocumentFileService,
    ManagedDocumentPathError,
)
from backend.app.security.retrieval_scope import RetrievalScope


def test_rejects_file_outside_managed_storage(
    tmp_path: Path,
) -> None:
    service = ManagedDocumentFileService(
        storage_dir=tmp_path / "documents"
    )
    scope = RetrievalScope(
        tenant_id=uuid4(),
        knowledge_base_id=uuid4(),
    )
    outside_file = tmp_path / "outside.txt"
    outside_file.write_bytes(b"protected")

    with pytest.raises(
        ManagedDocumentPathError,
        match="不在受管存储目录中",
    ):
        service.resolve_staged_uri(
            outside_file.resolve().as_uri(),
            scope=scope,
        )

    assert outside_file.read_bytes() == b"protected"


def test_rejects_staged_file_from_another_knowledge_base(
    tmp_path: Path,
) -> None:
    storage_dir = tmp_path / "documents"
    tenant_id = uuid4()
    allowed_scope = RetrievalScope(
        tenant_id=tenant_id,
        knowledge_base_id=uuid4(),
    )
    other_scope = RetrievalScope(
        tenant_id=tenant_id,
        knowledge_base_id=uuid4(),
    )
    other_staged_file = (
        storage_dir
        / str(other_scope.tenant_id)
        / str(other_scope.knowledge_base_id)
        / ".staging"
        / "candidate.txt"
    )
    other_staged_file.parent.mkdir(parents=True)
    other_staged_file.write_bytes(b"other-kb")
    service = ManagedDocumentFileService(
        storage_dir=storage_dir
    )

    with pytest.raises(
        ManagedDocumentPathError,
        match="当前知识库",
    ):
        service.resolve_staged_uri(
            other_staged_file.resolve().as_uri(),
            scope=allowed_scope,
        )

    assert other_staged_file.read_bytes() == b"other-kb"
