import os
from pathlib import Path

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(PROJECT_ROOT / ".env")


def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"缺少环境变量：{name}")
    return value


class Settings:
    def __init__(self):
        self.log_level = (
            os.getenv("LOG_LEVEL", "INFO").strip().upper()
            or "INFO"
        )
        self.database_url = required_env("DATABASE_URL")

        self.oidc_issuer = required_env("OIDC_ISSUER").rstrip("/")
        self.oidc_audience = required_env("OIDC_AUDIENCE")
        self.oidc_jwks_url = required_env("OIDC_JWKS_URL")

        self.chat_model = required_env("DASHSCOPE_MODEL")
        self.chat_base_url = required_env("DASHSCOPE_BASE_URL")
        self.chat_api_key = required_env("DASHSCOPE_API_KEY")

        self.summary_model = required_env("DEEPSEEK_MODEL")
        self.summary_base_url = required_env("DEEPSEEK_BASE_URL")
        self.summary_api_key = required_env("DEEPSEEK_API_KEY")

        self.oss_region = required_env("OSS_REGION")
        self.oss_endpoint = required_env("OSS_ENDPOINT")
        self.oss_access_key_id = required_env("OSS_ACCESS_KEY_ID")
        self.oss_access_key_secret = required_env("OSS_ACCESS_KEY_SECRET")
        self.oss_bucket = required_env("OSS_BUCKET")

        self.embedding_model = required_env("DASHSCOPE_EMBEDDING_MODEL")
        # 使用新文件，避免和正在运行的命令行脚本共用数据库
        self.database_path = PROJECT_ROOT / "weather_api.sqlite"

        self.rerank_model = required_env("DASHSCOPE_RERANK_MODEL")
        self.rerank_url = required_env("DASHSCOPE_RERANK_URL")
        self.rerank_timeout_seconds = float(required_env("RERANK_TIMEOUT_SECONDS"))

        self.document_storage_dir = Path(
            os.getenv(
                "DOCUMENT_STORAGE_DIR",
                str(PROJECT_ROOT / "data" / "documents"),
            )
        ).resolve()

        self.max_upload_bytes = int(
            os.getenv(
                "MAX_UPLOAD_BYTES",
                str(20 * 1024 * 1024),
            )
        )

        if self.max_upload_bytes < 1:
            raise RuntimeError(
                "MAX_UPLOAD_BYTES 必须是正整数"
            )

        self.indexing_worker_poll_seconds = float(
            os.getenv(
                "INDEXING_WORKER_POLL_SECONDS",
                "2",
            )
        )
        self.indexing_job_retry_delay_seconds = float(
            os.getenv(
                "INDEXING_JOB_RETRY_DELAY_SECONDS",
                "30",
            )
        )
        self.indexing_job_stale_after_seconds = float(
            os.getenv(
                "INDEXING_JOB_STALE_AFTER_SECONDS",
                "300",
            )
        )
        self.indexing_failed_file_retention_hours = float(
            os.getenv(
                "INDEXING_FAILED_FILE_RETENTION_HOURS",
                "24",
            )
        )
        self.indexing_worker_heartbeat_seconds = float(
            os.getenv(
                "INDEXING_WORKER_HEARTBEAT_SECONDS",
                "10",
            )
        )
        self.indexing_worker_stale_seconds = float(
            os.getenv(
                "INDEXING_WORKER_STALE_SECONDS",
                "30",
            )
        )
        self.indexing_worker_metrics_port = int(
            os.getenv(
                "INDEXING_WORKER_METRICS_PORT",
                "9101",
            )
        )

        if self.indexing_worker_poll_seconds <= 0:
            raise RuntimeError(
                "INDEXING_WORKER_POLL_SECONDS 必须大于 0"
            )

        if self.indexing_job_retry_delay_seconds < 0:
            raise RuntimeError(
                "INDEXING_JOB_RETRY_DELAY_SECONDS 不能小于 0"
            )

        if self.indexing_job_stale_after_seconds <= 0:
            raise RuntimeError(
                "INDEXING_JOB_STALE_AFTER_SECONDS 必须大于 0"
            )

        if self.indexing_failed_file_retention_hours <= 0:
            raise RuntimeError(
                "INDEXING_FAILED_FILE_RETENTION_HOURS "
                "必须大于 0"
            )

        if self.indexing_worker_heartbeat_seconds <= 0:
            raise RuntimeError(
                "INDEXING_WORKER_HEARTBEAT_SECONDS 必须大于 0"
            )

        if (
            self.indexing_worker_stale_seconds
            < self.indexing_worker_heartbeat_seconds * 2
        ):
            raise RuntimeError(
                "INDEXING_WORKER_STALE_SECONDS 至少应为心跳"
                "间隔的两倍"
            )

        if not 1 <= self.indexing_worker_metrics_port <= 65535:
            raise RuntimeError(
                "INDEXING_WORKER_METRICS_PORT 必须是有效端口"
            )
