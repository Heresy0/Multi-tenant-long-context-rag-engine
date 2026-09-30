import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

from backend.app.config import required_env


@pytest.mark.integration
def test_postgres_trigger_rejects_direct_audit_mutation(
) -> None:
    if (
        os.getenv("RUN_POSTGRES_INTEGRATION_TESTS")
        != "1"
    ):
        pytest.skip("未启用PostgreSQL集成测试")

    engine = create_engine(
        required_env("DATABASE_URL"),
        pool_pre_ping=True,
    )
    event_id = uuid4()

    try:
        with engine.connect() as connection:
            transaction = connection.begin()
            connection.execute(
                text(
                    """
                    INSERT INTO audit_events (
                        id,
                        tenant_id,
                        actor_user_id,
                        knowledge_base_id,
                        action,
                        resource_type,
                        resource_id,
                        outcome,
                        request_id,
                        details_json
                    ) VALUES (
                        :id,
                        :tenant_id,
                        NULL,
                        NULL,
                        'audit.trigger_test',
                        'audit_event',
                        :resource_id,
                        'success',
                        'integration-test',
                        '{}'
                    )
                    """
                ),
                {
                    "id": event_id,
                    "tenant_id": uuid4(),
                    "resource_id": str(event_id),
                },
            )

            with pytest.raises(
                DBAPIError,
                match="append-only",
            ):
                connection.execute(
                    text(
                        "UPDATE audit_events "
                        "SET outcome = 'failed' "
                        "WHERE id = :id"
                    ),
                    {"id": event_id},
                )

            transaction.rollback()
    finally:
        engine.dispose()
