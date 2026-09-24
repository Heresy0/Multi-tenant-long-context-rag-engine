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
        self.chroma_directory = PROJECT_ROOT / "data" / "chroma"
        # 使用新文件，避免和正在运行的命令行脚本共用数据库
        self.database_path = PROJECT_ROOT / "weather_api.sqlite"

        self.rerank_model = required_env("DASHSCOPE_RERANK_MODEL")
        self.rerank_url = required_env("DASHSCOPE_RERANK_URL")
        self.rerank_timeout_seconds = float(required_env("RERANK_TIMEOUT_SECONDS"))