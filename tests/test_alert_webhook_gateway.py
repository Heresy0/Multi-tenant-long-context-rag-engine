import requests
from fastapi.testclient import TestClient

from backend.app.alert_webhook_gateway import (
    REGISTRY,
    create_alert_webhook_app,
)


def _payload(*, secret: str = "not-exported") -> dict:
    return {
        "version": "4",
        "status": "firing",
        "receiver": "enterprise-webhook",
        "commonAnnotations": {
            "description": secret,
        },
        "alerts": [
            {
                "status": "firing",
                "labels": {
                    "alertname": "PipelineSmokeTest",
                    "severity": "warning",
                    "tenant_id": secret,
                },
            }
        ],
    }


def test_local_receiver_accepts_alert_without_storing_payload() -> None:
    secret = "tenant-secret-123"
    client = TestClient(create_alert_webhook_app())

    response = client.post(
        "/alerts",
        json=_payload(secret=secret),
    )
    status = client.get("/status").json()
    metrics = client.get("/metrics").text

    assert response.status_code == 200
    assert response.json() == {
        "status": "accepted",
        "forwarded": False,
        "alert_count": 1,
    }
    assert status["received_groups"] == 1
    assert status["received_alerts"] == 1
    assert status["forwarded_groups"] == 0
    assert secret not in metrics


def test_gateway_forwards_payload_and_authorization() -> None:
    recorded: dict = {}

    class FakeResponse:
        status_code = 202

        @staticmethod
        def raise_for_status() -> None:
            return None

    class FakeClient:
        @staticmethod
        def post(url, *, json, headers, timeout):
            recorded.update(
                url=url,
                json=json,
                headers=headers,
                timeout=timeout,
            )
            return FakeResponse()

    client = TestClient(
        create_alert_webhook_app(
            forward_url="https://alerts.example.test/hooks/1",
            authorization="Bearer hidden-token",
            timeout_seconds=3,
            http_client=FakeClient,
        )
    )
    payload = _payload()

    response = client.post("/alerts", json=payload)
    status = client.get("/status").json()

    assert response.status_code == 200
    assert response.json()["forwarded"] is True
    assert recorded == {
        "url": "https://alerts.example.test/hooks/1",
        "json": payload,
        "headers": {
            "Content-Type": "application/json",
            "Authorization": "Bearer hidden-token",
        },
        "timeout": 3,
    }
    assert status["forwarded_groups"] == 1
    assert status["failed_groups"] == 0


def test_gateway_returns_502_so_alertmanager_can_retry() -> None:
    class FailingClient:
        @staticmethod
        def post(*_args, **_kwargs):
            raise requests.ConnectionError("offline")

    client = TestClient(
        create_alert_webhook_app(
            forward_url="https://alerts.example.test/hooks/1",
            http_client=FailingClient,
        )
    )

    response = client.post("/alerts", json=_payload())
    status = client.get("/status").json()

    assert response.status_code == 502
    assert status["failed_groups"] == 1
    assert (
        REGISTRY.get_sample_value(
            "enterprise_alert_webhook_deliveries_total",
            {"outcome": "failed"},
        )
        or 0
    ) >= 1


def test_gateway_rejects_invalid_alerts_shape() -> None:
    client = TestClient(create_alert_webhook_app())

    response = client.post(
        "/alerts",
        json={"alerts": "not-a-list"},
    )

    assert response.status_code == 422
