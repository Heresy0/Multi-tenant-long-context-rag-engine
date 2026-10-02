import logging
from datetime import datetime
from uuid import UUID

from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from .db.models import AuditEvent
from .monitoring.observability import event_message


logger = logging.getLogger(__name__)

AUDIT_OUTCOMES = frozenset({
    "success",
    "denied",
    "invalid",
    "failed",
})
SENSITIVE_DETAIL_KEYS = frozenset({
    "answer",
    "authorization",
    "content",
    "file_path",
    "password",
    "question",
    "secret",
    "storage_uri",
    "token",
})
SAFE_DETAIL_KEYS = frozenset({
    "active",
    "answerable",
    "citation_count",
    "document_id",
    "document_version",
    "error_type",
    "indexing_job_id",
    "limit",
    "reason",
    "target_version",
    "requested",
    "used",
    "window_seconds",
})


def _validate_safe_details(
    value: object,
    *,
    path: str = "details",
) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            normalized_key = str(key).strip().lower()
            if normalized_key in SENSITIVE_DETAIL_KEYS:
                raise ValueError(
                    f"审计详情禁止包含敏感字段：{path}.{key}"
                )
            if normalized_key not in SAFE_DETAIL_KEYS:
                raise ValueError(
                    f"审计详情字段未获允许：{path}.{key}"
                )
            _validate_safe_details(
                child,
                path=f"{path}.{key}",
            )
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _validate_safe_details(
                child,
                path=f"{path}[{index}]",
            )


class AuditService:
    """在租户范围内追加和查询安全审计事件。"""

    def __init__(self, session: Session) -> None:
        self._session = session

    def add(
        self,
        *,
        tenant_id: UUID,
        actor_user_id: UUID | None,
        action: str,
        resource_type: str,
        outcome: str,
        knowledge_base_id: UUID | None = None,
        resource_id: UUID | str | None = None,
        request_id: str | None = None,
        details: dict[str, object] | None = None,
    ) -> AuditEvent:
        """把事件加入当前事务，但不自行提交。"""
        normalized_action = action.strip()
        normalized_resource_type = resource_type.strip()

        if not normalized_action:
            raise ValueError("审计动作不能为空。")

        if not normalized_resource_type:
            raise ValueError("审计资源类型不能为空。")

        if outcome not in AUDIT_OUTCOMES:
            raise ValueError("审计结果不合法。")

        normalized_details = dict(details or {})
        _validate_safe_details(normalized_details)

        event = AuditEvent(
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            knowledge_base_id=knowledge_base_id,
            action=normalized_action,
            resource_type=normalized_resource_type,
            resource_id=(
                str(resource_id)
                if resource_id is not None
                else None
            ),
            outcome=outcome,
            request_id=(
                request_id.strip()[:64]
                if request_id and request_id.strip()
                else None
            ),
            details_json=normalized_details,
        )
        self._session.add(event)
        self._session.flush()
        return event

    def record(self, **fields) -> AuditEvent:
        """在独立审计事务中追加并提交一条事件。"""
        try:
            audit_event = self.add(**fields)
            self._session.commit()
            log_committed_audit_event(audit_event)
            return audit_event
        except Exception:
            self._session.rollback()
            raise

    def list_for_knowledge_base(
        self,
        *,
        tenant_id: UUID,
        knowledge_base_id: UUID,
        action: str | None = None,
        outcome: str | None = None,
        before: datetime | None = None,
        limit: int = 50,
    ) -> list[AuditEvent]:
        """按租户和知识库强制过滤审计事件。"""
        if not 1 <= limit <= 100:
            raise ValueError("limit 必须介于 1 和 100 之间。")

        statement: Select[tuple[AuditEvent]] = select(
            AuditEvent
        ).where(
            AuditEvent.tenant_id == tenant_id,
            AuditEvent.knowledge_base_id
            == knowledge_base_id,
        )

        if action is not None:
            statement = statement.where(
                AuditEvent.action == action
            )

        if outcome is not None:
            if outcome not in AUDIT_OUTCOMES:
                raise ValueError("审计结果不合法。")
            statement = statement.where(
                AuditEvent.outcome == outcome
            )

        if before is not None:
            statement = statement.where(
                AuditEvent.occurred_at < before
            )

        return list(
            self._session.scalars(
                statement.order_by(
                    AuditEvent.occurred_at.desc(),
                    AuditEvent.id.desc(),
                ).limit(limit)
            ).all()
        )


def log_committed_audit_event(event: AuditEvent) -> None:
    """把已提交事件同步写入结构化应用日志，供 Loki 检索。"""
    try:
        logger.info(
            event_message(
                "audit.committed",
                audit_event_id=str(event.id),
                tenant_id=str(event.tenant_id),
                actor_user_id=(
                    str(event.actor_user_id)
                    if event.actor_user_id is not None
                    else None
                ),
                knowledge_base_id=(
                    str(event.knowledge_base_id)
                    if event.knowledge_base_id is not None
                    else None
                ),
                action=event.action,
                resource_type=event.resource_type,
                resource_id=event.resource_id,
                outcome=event.outcome,
                request_id=event.request_id,
            )
        )
    except Exception:
        # 权威审计记录已经提交，日志输出失败不能反向破坏
        # 已完成的业务事务。
        logger.exception(
            "已提交审计事件的结构化日志输出失败"
        )
