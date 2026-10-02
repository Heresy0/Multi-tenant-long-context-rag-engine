import os

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from backend.app.config import required_env
from backend.app.db.models import (
    KnowledgeBase,
    KnowledgeDocument,
    Tenant,
)
from backend.app.governance.tenant_document_quota_service import (
    TenantDocumentQuotaExceeded,
    TenantDocumentQuotaService,
)


@pytest.mark.integration
def test_postgres_tenant_lock_prevents_concurrent_quota_oversell(
) -> None:
    if os.getenv("RUN_POSTGRES_INTEGRATION_TESTS") != "1":
        pytest.skip("未启用PostgreSQL集成测试")

    engine = create_engine(
        required_env("DATABASE_URL"),
        pool_pre_ping=True,
    )
    tenant_id = uuid4()
    knowledge_base_id = uuid4()

    with Session(engine) as session:
        session.add(
            Tenant(
                id=tenant_id,
                name=f"并发配额测试-{tenant_id}",
                max_document_count=1,
                max_storage_bytes=1024,
            )
        )
        session.flush()
        session.add(
            KnowledgeBase(
                id=knowledge_base_id,
                tenant_id=tenant_id,
                name="并发配额知识库",
                visibility="restricted",
            )
        )
        session.commit()

    barrier = Barrier(2)

    def upload(file_name: str) -> str:
        with Session(engine) as session:
            service = TenantDocumentQuotaService(session)
            barrier.wait(timeout=10)
            try:
                tenant = service.lock_tenant(tenant_id)
                service.require_capacity(
                    tenant=tenant,
                    candidate_size_bytes=len(file_name),
                    creates_document=True,
                )
                session.add(
                    KnowledgeDocument(
                        tenant_id=tenant_id,
                        knowledge_base_id=knowledge_base_id,
                        created_by_user_id=None,
                        source_id=uuid4().hex + uuid4().hex,
                        file_name=file_name,
                        storage_uri=f"file:///{file_name}",
                        mime_type="text/plain",
                        content_hash=(
                            uuid4().hex + uuid4().hex
                        ),
                        file_size_bytes=0,
                        status="pending",
                        version=1,
                        metadata_json={},
                    )
                )
                session.commit()
                return "accepted"
            except TenantDocumentQuotaExceeded as exc:
                session.rollback()
                return exc.resource

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(
                executor.map(
                    upload,
                    ["first.txt", "second.txt"],
                )
            )

        assert sorted(outcomes) == [
            "accepted",
            "documents",
        ]

        with Session(engine) as session:
            assert session.scalar(
                select(func.count())
                .select_from(KnowledgeDocument)
                .where(
                    KnowledgeDocument.tenant_id
                    == tenant_id
                )
            ) == 1
    finally:
        with Session(engine) as session:
            tenant = session.get(Tenant, tenant_id)
            if tenant is not None:
                session.delete(tenant)
            session.commit()
        engine.dispose()
