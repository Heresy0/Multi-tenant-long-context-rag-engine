import os
import random
import sys

from pathlib import Path

from locust import HttpUser, between, task


PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.load_test_support import (  # noqa: E402
    LoadTestConfig,
    validate_qa_response,
)


CONFIG = LoadTestConfig.from_environment(
    os.environ,
    project_root=PROJECT_ROOT,
)


class EnterpriseKnowledgeQaUser(HttpUser):
    """使用真实 JWT 重复执行受租户治理保护的 QA 请求。"""

    host = CONFIG.base_url
    wait_time = between(
        CONFIG.wait_min_seconds,
        CONFIG.wait_max_seconds,
    )

    def on_start(self) -> None:
        self.headers = {
            "Authorization": (
                f"Bearer {CONFIG.access_token}"
            ),
            "Content-Type": "application/json",
        }

    @task
    def ask_knowledge_base(self) -> None:
        question = random.choice(CONFIG.questions)
        with self.client.post(
            "/api/qa",
            headers=self.headers,
            json={
                "knowledge_base_id": str(
                    CONFIG.knowledge_base_id
                ),
                "question": question,
            },
            name="/api/qa [pending]",
            timeout=CONFIG.request_timeout_seconds,
            catch_response=True,
        ) as response:
            try:
                payload = response.json()
            except ValueError:
                payload = None

            validation = validate_qa_response(
                status_code=response.status_code,
                payload=payload,
                request_id=response.headers.get(
                    "X-Request-ID"
                ),
                mode=CONFIG.mode,
                response_headers=response.headers,
            )
            response.request_meta["name"] = validation.name

            if validation.successful:
                response.success()
            else:
                response.failure(
                    validation.message
                    or "QA 响应校验失败"
                )
