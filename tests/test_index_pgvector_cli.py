import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy.exc import SQLAlchemyError

from scripts import index_pgvector as cli

from backend.app.security.authorization import (
    AuthorizationDenied,
)
from backend.app.security.retrieval_scope import (
    RetrievalScope,
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
    assert "将本地文件或目录批量写入指定的" in (
        result.stdout
    )
    assert "--directory" in result.stdout
    assert "--recursive" in result.stdout


def test_file_and_directory_are_mutually_exclusive(
    tmp_path: Path,
) -> None:
    result = run_cli(
        "--file",
        str(__file__),
        "--directory",
        str(tmp_path),
        "--tenant-id",
        VALID_UUID,
        "--knowledge-base-id",
        VALID_UUID,
        "--user-id",
        VALID_UUID,
    )

    assert result.returncode == 2
    assert "not allowed with argument" in result.stderr


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


def test_discovers_supported_files_in_stable_order(
    tmp_path: Path,
) -> None:
    nested = tmp_path / "nested"
    nested.mkdir()
    (tmp_path / "b.txt").write_text("b", encoding="utf-8")
    (tmp_path / "A.docx").write_bytes(b"docx")
    (tmp_path / "ignored.exe").write_bytes(b"exe")
    (nested / "c.pdf").write_bytes(b"pdf")
    (nested / "d.md").write_text("d", encoding="utf-8")

    shallow = cli.discover_files(
        file_path=None,
        directory_path=tmp_path,
        recursive=False,
    )
    recursive = cli.discover_files(
        file_path=None,
        directory_path=tmp_path,
        recursive=True,
    )

    assert [path.name for path in shallow] == [
        "A.docx",
        "b.txt",
    ]
    assert [path.name for path in recursive] == [
        "A.docx",
        "b.txt",
        "c.pdf",
        "d.md",
    ]


def test_batch_summary_keeps_processing_after_failure(
    tmp_path: Path,
) -> None:
    file_paths = [
        tmp_path / "updated.txt",
        tmp_path / "unchanged.txt",
        tmp_path / "broken.docx",
    ]
    scope = RetrievalScope(
        tenant_id=uuid4(),
        knowledge_base_id=uuid4(),
    )
    user_id = uuid4()

    class FakeIndexingService:
        def __init__(self) -> None:
            self.outcomes = iter([
                4,
                0,
                ValueError("文档损坏"),
            ])
            self.calls: list[dict] = []

        def index_file(self, **kwargs) -> int:
            self.calls.append(kwargs)
            outcome = next(self.outcomes)

            if isinstance(outcome, Exception):
                raise outcome

            return outcome

    service = FakeIndexingService()

    summary = cli.index_paths(
        indexing_service=service,
        file_paths=file_paths,
        scope=scope,
        created_by_user_id=user_id,
        continue_on_error=True,
    )

    assert summary.discovered_count == 3
    assert summary.indexed_count == 1
    assert summary.skipped_count == 1
    assert summary.failed_count == 1
    assert summary.chunk_count == 4
    assert summary.failures[0].file_path == file_paths[2]
    assert len(service.calls) == 3
    assert all(
        call["scope"] == scope
        and call["created_by_user_id"] == user_id
        for call in service.calls
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
