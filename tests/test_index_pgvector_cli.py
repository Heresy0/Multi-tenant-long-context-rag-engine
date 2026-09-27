import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy.exc import SQLAlchemyError

from scripts import index_pgvector as cli

from backend.app.security.authorization import (
    AuthorizationDenied,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = (
    PROJECT_ROOT / "scripts" / "index_pgvector.py"
)
VALID_UUID = "00000000-0000-0000-0000-000000000001"


def run_cli(*arguments: str) -> subprocess.CompletedProcess[str]:
    environment = {
        **os.environ,
        "PYTHONUTF8": "1",
    }

    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            *arguments,
        ],
        cwd=PROJECT_ROOT,
        env=environment,
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
    )


def test_help_can_be_displayed() -> None:
    result = run_cli("--help")

    assert result.returncode == 0
    assert "将本地文件写入指定的 pgvector 知识库" in (
        result.stdout
    )


def test_invalid_tenant_id_is_rejected() -> None:
    result = run_cli(
        "--file",
        str(__file__),
        "--tenant-id",
        "not-a-uuid",
        "--knowledge-base-id",
        VALID_UUID,
        "--user-id",
        VALID_UUID,
    )

    assert result.returncode == 2
    assert "invalid UUID value" in result.stderr


def test_missing_file_is_rejected(
    tmp_path: Path,
) -> None:
    missing_file = tmp_path / "missing.docx"

    result = run_cli(
        "--file",
        str(missing_file),
        "--tenant-id",
        VALID_UUID,
        "--knowledge-base-id",
        VALID_UUID,
        "--user-id",
        VALID_UUID,
    )

    assert result.returncode == 1
    assert "文件不存在或不是普通文件" in (
        result.stderr
    )


def test_database_error_has_friendly_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail() -> None:
        raise SQLAlchemyError("sensitive technical detail")

    monkeypatch.setattr(cli, "run", fail)

    with pytest.raises(SystemExit) as captured:
        cli.main()

    message = str(captured.value)

    assert "数据库连接或操作失败" in message
    assert "PostgreSQL" in message
    assert "sensitive technical detail" not in message


def test_authorization_error_has_friendly_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail() -> None:
        raise AuthorizationDenied("denied")

    monkeypatch.setattr(cli, "run", fail)

    with pytest.raises(SystemExit) as captured:
        cli.main()

    assert "至少需要 editor 权限" in str(
        captured.value
    )
