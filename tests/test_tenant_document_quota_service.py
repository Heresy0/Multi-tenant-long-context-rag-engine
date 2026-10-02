from collections.abc import Iterator
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from backend.app.db.base import Base
from backend.app.db.models import (
    DocumentIndexingJob,
    KnowledgeBase,
    KnowledgeDocument,
    Tenant,
    User,
)
from backend.app.governance.tenant_document_quota_service import (
    TenantDocumentQuotaExceeded,
    TenantDocumentQuotaService,
)


@pytest.fixture
def session() -> Iterator[Session]:
    engine: Engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
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


def _seed_usage(session: Session) -> Tenant:
    tenant = Tenant(
        id=uuid4(),
        name="配额测试企业",
        max_document_count=2,
        max_storage_bytes=100,
    )
    user = User(
        id=uuid4(),
        tenant_id=tenant.id,
        external_subject="quota-user",
        name="Quota User",
    )
    knowledge_base = KnowledgeBase(
        id=uuid4(),
        tenant_id=tenant.id,
        name="配额知识库",
        visibility="restricted",
    )
    session.add(tenant)
    session.flush()
    session.add_all([user, knowledge_base])
    session.flush()
    document = KnowledgeDocument(
        id=uuid4(),
        tenant_id=tenant.id,
        knowledge_base_id=knowledge_base.id,
        created_by_user_id=user.id,
        source_id="a" * 64,
        file_name="existing.txt",
        storage_uri="file:///existing.txt",
        mime_type="text/plain",
        content_hash="b" * 64,
        file_size_bytes=30,
        status="ready",
        version=1,
        metadata_json={},
    )
    session.add(document)
    session.flush()
    session.add(
        DocumentIndexingJob(
            tenant_id=tenant.id,
            knowledge_base_id=knowledge_base.id,
            document_id=document.id,
            requested_by_user_id=user.id,
            staged_storage_uri="file:///candidate.txt",
            candidate_file_name="existing.txt",
            candidate_mime_type="text/plain",
            candidate_content_hash="c" * 64,
            candidate_size_bytes=20,
            target_version=2,
            status="queued",
            attempt_count=0,
            max_attempts=3,
        )
    )
    session.commit()
    return tenant


def test_snapshot_counts_stored_and_reserved_bytes(
    session: Session,
) -> None:
    tenant = _seed_usage(session)
    service = TenantDocumentQuotaService(session)
    locked = service.lock_tenant(tenant.id)

    snapshot = service.require_capacity(
        tenant=locked,
        candidate_size_bytes=50,
        creates_document=False,
    )

    assert snapshot.document_count == 1
    assert snapshot.stored_bytes == 30
    assert snapshot.reserved_bytes == 20
    assert snapshot.used_storage_bytes == 50


def test_rejects_storage_and_document_count_overages(
    session: Session,
) -> None:
    tenant = _seed_usage(session)
    service = TenantDocumentQuotaService(session)
    locked = service.lock_tenant(tenant.id)

    with pytest.raises(
        TenantDocumentQuotaExceeded
    ) as storage_error:
        service.require_capacity(
            tenant=locked,
            candidate_size_bytes=51,
            creates_document=False,
        )
    assert storage_error.value.resource == "storage_bytes"
    assert storage_error.value.used == 50

    locked.max_document_count = 1
    with pytest.raises(
        TenantDocumentQuotaExceeded
    ) as count_error:
        service.require_capacity(
            tenant=locked,
            candidate_size_bytes=1,
            creates_document=True,
        )
    assert count_error.value.resource == "documents"
    assert count_error.value.used == 1


def test_released_reservation_is_not_counted(
    session: Session,
) -> None:
    tenant = _seed_usage(session)
    job = session.query(DocumentIndexingJob).one()
    job.reservation_released_at = datetime.now(
        timezone.utc
    )
    session.commit()

    service = TenantDocumentQuotaService(session)
    snapshot = service.snapshot(
        tenant=service.lock_tenant(tenant.id)
    )

    assert snapshot.stored_bytes == 30
    assert snapshot.reserved_bytes == 0
    assert snapshot.used_storage_bytes == 30
