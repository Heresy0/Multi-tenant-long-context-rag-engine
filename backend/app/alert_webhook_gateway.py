import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from threading import Lock
from time import perf_counter
from typing import Any
from urllib.parse import urlparse

import requests
from fastapi import FastAPI, HTTPException
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Histogram,
    REGISTRY,
    generate_latest,
)
from starlette.responses import Response

from .observability import event_message


logger = logging.getLogger(__name__)
logger.setLevel(
    os.getenv("LOG_LEVEL", "INFO").strip().upper()
)
if not logger.handlers:
    log_handler = logging.StreamHandler()
    log_handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(log_handler)
logger.propagate = False

ALERT_DELIVERIES = Counter(
    "enterprise_alert_webhook_deliveries_total",
    "告警 Webhook 网关处理的通知组数量。",
    ("outcome",),
)
ALERTS_RECEIVED = Counter(
    "enterprise_alert_webhook_alerts_total",
    "告警 Webhook 网关收到的告警数量。",
    ("status", "severity"),
)
ALERT_FORWARD_DURATION = Histogram(
    "enterprise_alert_webhook_forward_duration_seconds",
    "告警 Webhook 网关向外部端点转发的耗时。",
    ("outcome",),
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 20),
)

KNOWN_STATUSES = frozenset({"firing", "resolved"})
KNOWN_SEVERITIES = frozenset(
    {"critical", "warning", "info"}
)


def _bounded_label(
    value: object,
    *,
    known_values: frozenset[str],
) -> str:
    normalized = str(value or "unknown").strip().lower()

    if normalized in known_values:
        return normalized

    if normalized in {"", "unknown"}:
        return "unknown"

    return "other"


def _optional_http_url(value: str | None) -> str | None:
    normalized = (value or "").strip()

    if not normalized:
        return None

    parsed = urlparse(normalized)

    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise RuntimeError(
            "ALERT_WEBHOOK_FORWARD_URL 必须是有效的 HTTP(S) URL"
        )

    return normalized


def _positive_timeout(value: str | None) -> float:
    timeout = float((value or "10").strip())

    if timeout <= 0:
        raise RuntimeError(
            "ALERT_WEBHOOK_TIMEOUT_SECONDS 必须大于 0"
        )

    return timeout


@dataclass
class GatewayState:
    received_groups: int = 0
    received_alerts: int = 0
    forwarded_groups: int = 0
    failed_groups: int = 0
    last_received_at: datetime | None = None
    lock: Lock = field(default_factory=Lock, repr=False)

    def record_received(self, alert_count: int) -> None:
        with self.lock:
            self.received_groups += 1
            self.received_alerts += alert_count
            self.last_received_at = datetime.now(timezone.utc)

    def record_forwarded(self) -> None:
        with self.lock:
            self.forwarded_groups += 1

    def record_failed(self) -> None:
        with self.lock:
            self.failed_groups += 1

    def snapshot(self) -> dict[str, int | str | None]:
        with self.lock:
            return {
                "received_groups": self.received_groups,
                "received_alerts": self.received_alerts,
                "forwarded_groups": self.forwarded_groups,
                "failed_groups": self.failed_groups,
                "last_received_at": (
                    self.last_received_at.isoformat()
                    if self.last_received_at is not None
                    else None
                ),
            }


def create_alert_webhook_app(
    *,
    forward_url: str | None = None,
    authorization: str | None = None,
    timeout_seconds: float = 10,
    http_client: Any = requests,
) -> FastAPI:
    """创建独立告警网关，不保存告警正文和业务标签。"""
    validated_forward_url = _optional_http_url(forward_url)

    if timeout_seconds <= 0:
        raise RuntimeError("Webhook 转发超时必须大于 0")

    gateway_state = GatewayState()
    gateway = FastAPI(
        title="Enterprise Knowledge Alert Webhook Gateway",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    gateway.state.gateway_state = gateway_state

    @gateway.get("/health")
    def health() -> dict[str, str | bool]:
        return {
            "status": "ok",
            "forwarding_enabled": (
                validated_forward_url is not None
            ),
        }

    @gateway.get("/status")
    def status() -> dict[str, int | str | bool | None]:
        return {
            "status": "ok",
            "forwarding_enabled": (
                validated_forward_url is not None
            ),
            **gateway_state.snapshot(),
        }

    @gateway.get("/metrics", include_in_schema=False)
    def metrics() -> Response:
        return Response(
            content=generate_latest(REGISTRY),
            media_type=CONTENT_TYPE_LATEST,
        )

    @gateway.post("/alerts")
    def receive_alerts(
        payload: dict[str, Any],
    ) -> dict[str, str | bool | int]:
        raw_alerts = payload.get("alerts", [])

        if not isinstance(raw_alerts, list):
            raise HTTPException(
                status_code=422,
                detail="alerts 必须是数组。",
            )

        gateway_state.record_received(len(raw_alerts))

        for raw_alert in raw_alerts:
            alert = (
                raw_alert
                if isinstance(raw_alert, Mapping)
                else {}
            )
            labels = alert.get("labels", {})
            labels = (
                labels
                if isinstance(labels, Mapping)
                else {}
            )
            alert_status = _bounded_label(
                alert.get("status"),
                known_values=KNOWN_STATUSES,
            )
            severity = _bounded_label(
                labels.get("severity"),
                known_values=KNOWN_SEVERITIES,
            )
            ALERTS_RECEIVED.labels(
                status=alert_status,
                severity=severity,
            ).inc()

        common_fields = {
            "notification_status": _bounded_label(
                payload.get("status"),
                known_values=KNOWN_STATUSES,
            ),
            "alert_count": len(raw_alerts),
            "forwarding_enabled": (
                validated_forward_url is not None
            ),
        }

        if validated_forward_url is None:
            ALERT_DELIVERIES.labels(outcome="accepted").inc()
            logger.info(
                event_message(
                    "alert.notification.received",
                    **common_fields,
                )
            )
            return {
                "status": "accepted",
                "forwarded": False,
                "alert_count": len(raw_alerts),
            }

        headers = {"Content-Type": "application/json"}

        if authorization and authorization.strip():
            headers["Authorization"] = authorization.strip()

        started = perf_counter()

        try:
            forwarded_response = http_client.post(
                validated_forward_url,
                json=payload,
                headers=headers,
                timeout=timeout_seconds,
            )
            forwarded_response.raise_for_status()
        except requests.RequestException as exc:
            gateway_state.record_failed()
            ALERT_DELIVERIES.labels(outcome="failed").inc()
            ALERT_FORWARD_DURATION.labels(
                outcome="failed"
            ).observe(perf_counter() - started)
            logger.warning(
                event_message(
                    "alert.notification.forward_failed",
                    **common_fields,
                    error_type=type(exc).__name__,
                )
            )
            raise HTTPException(
                status_code=502,
                detail="外部告警 Webhook 暂时不可用。",
            ) from exc

        gateway_state.record_forwarded()
        ALERT_DELIVERIES.labels(outcome="forwarded").inc()
        ALERT_FORWARD_DURATION.labels(
            outcome="forwarded"
        ).observe(perf_counter() - started)
        logger.info(
            event_message(
                "alert.notification.forwarded",
                **common_fields,
                external_status_code=(
                    forwarded_response.status_code
                ),
            )
        )
        return {
            "status": "accepted",
            "forwarded": True,
            "alert_count": len(raw_alerts),
        }

    return gateway


app = create_alert_webhook_app(
    forward_url=os.getenv("ALERT_WEBHOOK_FORWARD_URL"),
    authorization=os.getenv("ALERT_WEBHOOK_AUTHORIZATION"),
    timeout_seconds=_positive_timeout(
        os.getenv("ALERT_WEBHOOK_TIMEOUT_SECONDS")
    ),
)
