import argparse
import json
import time
from datetime import datetime, timedelta, timezone

import requests


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "注入并恢复一条临时告警，验证 Alertmanager 到 "
            "Webhook 网关的通知链路。"
        )
    )
    parser.add_argument(
        "--alertmanager-url",
        default="http://127.0.0.1:9093",
    )
    parser.add_argument(
        "--gateway-url",
        default="http://127.0.0.1:8090",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=20,
    )
    return parser.parse_args()


def _gateway_status(
    *,
    gateway_url: str,
    timeout_seconds: float,
) -> dict:
    response = requests.get(
        f"{gateway_url.rstrip('/')}/status",
        timeout=timeout_seconds,
    )
    response.raise_for_status()
    return response.json()


def _send_alert(
    *,
    alertmanager_url: str,
    started_at: datetime,
    ends_at: datetime,
    timeout_seconds: float,
) -> None:
    response = requests.post(
        f"{alertmanager_url.rstrip('/')}/api/v2/alerts",
        json=[
            {
                "labels": {
                    "alertname": "AlertPipelineSmokeTest",
                    "severity": "critical",
                    "service": "alert-pipeline",
                },
                "annotations": {
                    "summary": (
                        "Alertmanager notification pipeline "
                        "smoke test"
                    ),
                    "description": (
                        "Temporary validation alert; the test "
                        "script resolves it automatically."
                    ),
                },
                "startsAt": started_at.isoformat(),
                "endsAt": ends_at.isoformat(),
                "generatorURL": (
                    "http://127.0.0.1:9090/alerts"
                ),
            }
        ],
        timeout=timeout_seconds,
    )
    response.raise_for_status()


def main() -> int:
    args = _parse_args()

    if args.timeout_seconds <= 0:
        raise SystemExit("--timeout-seconds 必须大于 0")

    initial = _gateway_status(
        gateway_url=args.gateway_url,
        timeout_seconds=args.timeout_seconds,
    )
    initial_groups = int(initial["received_groups"])
    started_at = datetime.now(timezone.utc)

    try:
        _send_alert(
            alertmanager_url=args.alertmanager_url,
            started_at=started_at,
            ends_at=started_at + timedelta(minutes=5),
            timeout_seconds=args.timeout_seconds,
        )

        deadline = time.monotonic() + args.timeout_seconds

        while time.monotonic() < deadline:
            current = _gateway_status(
                gateway_url=args.gateway_url,
                timeout_seconds=args.timeout_seconds,
            )

            if int(current["received_groups"]) > initial_groups:
                print(
                    json.dumps(
                        {
                            "status": "passed",
                            "before": initial,
                            "after": current,
                        },
                        ensure_ascii=False,
                        indent=2,
                    )
                )
                return 0

            time.sleep(1)

        print(
            json.dumps(
                {
                    "status": "failed",
                    "reason": "等待 Webhook 通知超时",
                    "before": initial,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 1

    finally:
        _send_alert(
            alertmanager_url=args.alertmanager_url,
            started_at=started_at,
            ends_at=datetime.now(timezone.utc),
            timeout_seconds=args.timeout_seconds,
        )


if __name__ == "__main__":
    raise SystemExit(main())
