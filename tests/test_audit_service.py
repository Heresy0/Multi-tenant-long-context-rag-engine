import json
import logging
from collections.abc import Iterator
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from backend.app.audit_service import AuditService
from backend.app.db.base import Base
from backend.app.db.models import AuditEvent


@pytest.fixture
def session() -> Iterator[Session]:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)

    with Session(engine, expire_on_commit=False) as value:
        yield value

    Base.metadata.drop_all(engine)
    engine.dispose()


def test_records_and_lists_only_requested_scope(
    session: Session,
) -> None:
    tenant_id = uuid4()
    other_tenant_id = uuid4()
    knowledge_base_id = uuid4()
    other_knowledge_base_id = uuid4()
    actor_user_id = uuid4()
    service = AuditService(session)

    expected = service.record(
        tenant_id=tenant_id,
        actor_user_id=actor_user_id,
        knowledge_base_id=knowledge_base_id,
        action="document.upload_requested",
        resource_type="document",
        resource_id=uuid4(),
        outcome="success",
        request_id="request-1",
        details={"target_version": 2},
    )
    service.record(
        tenant_id=tenant_id,
        actor_user_id=actor_user_id,
        knowledge_base_id=other_knowledge_base_id,
        action="document.deleted",
        resource_type="document",
        outcome="success",
    )
    service.record(
        tenant_id=other_tenant_id,
        actor_user_id=uuid4(),
        knowledge_base_id=knowledge_base_id,
        action="qa.queried",
        resource_type="knowledge_base",
        outcome="denied",
    )

    events = service.list_for_knowledge_base(
        tenant_id=tenant_id,
        knowledge_base_id=knowledge_base_id,
    )

    assert [event.id for event in events] == [
        expected.id
    ]
    assert events[0].details_json == {
        "target_version": 2
    }


def test_rejects_sensitive_audit_details(
    session: Session,
) -> None:
    with pytest.raises(
        ValueError,
        match="敏感字段",
    ):
        AuditService(session).add(
            tenant_id=uuid4(),
            actor_user_id=uuid4(),
            action="qa.queried",
            resource_type="knowledge_base",
            outcome="success",
            details={"question": "不能进入审计日志"},
        )


def test_committed_event_emits_searchable_json_log(
    session: Session,
    caplog,
) -> None:
    caplog.set_level(
        logging.INFO,
        logger="backend.app.audit_service",
    )

    event = AuditService(session).record(
        tenant_id=uuid4(),
        actor_user_id=uuid4(),
        knowledge_base_id=uuid4(),
        action="document.deleted",
        resource_type="document",
        resource_id=uuid4(),
        outcome="success",
        request_id="request-for-loki",
    )

    messages = [
        json.loads(record.getMessage())
        for record in caplog.records
        if record.name == "backend.app.audit_service"
    ]
    assert messages == [{
        "event": "audit.committed",
        "audit_event_id": str(event.id),
        "tenant_id": str(event.tenant_id),
        "actor_user_id": str(event.actor_user_id),
        "knowledge_base_id": str(
            event.knowledge_base_id
        ),
        "action": "document.deleted",
        "resource_type": "document",
        "resource_id": event.resource_id,
        "outcome": "success",
        "request_id": "request-for-loki",
    }]


def test_orm_rejects_update_and_delete(
    session: Session,
) -> None:
    event = AuditService(session).record(
        tenant_id=uuid4(),
        actor_user_id=uuid4(),
        action="qa.queried",
        resource_type="knowledge_base",
        outcome="success",
    )

    event.outcome = "failed"
    with pytest.raises(ValueError, match="不可变"):
        session.commit()
    session.rollback()

    stored = session.scalar(
        select(AuditEvent).where(
            AuditEvent.id == event.id
        )
    )
    assert stored is not None
    assert stored.outcome == "success"

    session.delete(stored)
    with pytest.raises(ValueError, match="不可变"):
        session.commit()
    session.rollback()

    assert session.get(AuditEvent, event.id) is not None
