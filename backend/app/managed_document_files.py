import hashlib
from dataclasses import dataclass
from os import name as operating_system
from pathlib import Path
from urllib.parse import unquote, urlparse
from uuid import UUID

from .security.retrieval_scope import RetrievalScope


READ_BLOCK_SIZE = 1024 * 1024


class ManagedDocumentPathError(ValueError):
    """任务引用了不安全或无法识别的文件路径。"""


@dataclass(frozen=True, slots=True)
class CandidateFileSwap:
    staged_path: Path
    target_path: Path
    backup_path: Path
    candidate_content_hash: str


class ManagedDocumentFileService:
    """在受管目录内提升、恢复并清理候选文件。"""

    def __init__(
        self,
        *,
        storage_dir: str | Path,
    ) -> None:
        self._storage_dir = (
            Path(storage_dir)
            .expanduser()
            .resolve()
        )

    def prepare_candidate(
        self,
        *,
        job_id: UUID,
        scope: RetrievalScope,
        staged_storage_uri: str,
        target_storage_uri: str,
        candidate_content_hash: str,
    ) -> CandidateFileSwap:
        staged_path = self.resolve_staged_uri(
            staged_storage_uri,
            scope=scope,
        )
        target_path = self.resolve_target_uri(
            target_storage_uri,
            scope=scope,
        )
        backup_path = (
            target_path.parent
            / f".backup-{job_id}.tmp"
        )

        if target_path.is_file() and (
            self._calculate_hash(target_path)
            == candidate_content_hash
        ):
            # Worker 可能在索引提交后、任务状态提交前退出。
            # 此时正式位置已经是候选文件，直接继续即可。
            return CandidateFileSwap(
                staged_path=staged_path,
                target_path=target_path,
                backup_path=backup_path,
                candidate_content_hash=(
                    candidate_content_hash
                ),
            )

        if not staged_path.is_file():
            raise FileNotFoundError(
                f"索引候选文件不存在：{staged_path}"
            )

        if (
            self._calculate_hash(staged_path)
            != candidate_content_hash
        ):
            raise ManagedDocumentPathError(
                "暂存候选文件内容哈希不匹配"
            )

        if target_path.is_file():
            # 若上次恢复留下了备份，以当前正式文件为准，
            # 重新建立本次任务的确定性备份。
            backup_path.unlink(missing_ok=True)
            target_path.replace(backup_path)

        staged_path.replace(target_path)

        return CandidateFileSwap(
            staged_path=staged_path,
            target_path=target_path,
            backup_path=backup_path,
            candidate_content_hash=candidate_content_hash,
        )

    def restore_candidate(
        self,
        swap: CandidateFileSwap,
    ) -> None:
        """索引失败后恢复旧文件，并保留候选文件供重试。"""
        if (
            swap.target_path.is_file()
            and self._calculate_hash(swap.target_path)
            == swap.candidate_content_hash
        ):
            swap.staged_path.parent.mkdir(
                parents=True,
                exist_ok=True,
            )

            if swap.staged_path.exists():
                swap.target_path.unlink()
            else:
                swap.target_path.replace(
                    swap.staged_path
                )

        if swap.backup_path.is_file():
            if swap.target_path.exists():
                swap.target_path.unlink()

            swap.backup_path.replace(
                swap.target_path
            )

    def complete_candidate(
        self,
        swap: CandidateFileSwap,
    ) -> None:
        """任务成功后移除备份和残留候选文件。"""
        swap.backup_path.unlink(missing_ok=True)
        swap.staged_path.unlink(missing_ok=True)
        self._remove_empty_directory(
            swap.staged_path.parent
        )

    def discard_staged_candidate(
        self,
        *,
        staged_storage_uri: str,
        scope: RetrievalScope,
    ) -> None:
        staged_path = self.resolve_staged_uri(
            staged_storage_uri,
            scope=scope,
        )
        staged_path.unlink(missing_ok=True)
        self._remove_empty_directory(
            staged_path.parent
        )

    def require_staged_candidate(
        self,
        *,
        staged_storage_uri: str,
        scope: RetrievalScope,
        expected_content_hash: str,
    ) -> Path:
        """确认人工重试所需的候选文件仍存在且内容未变。"""
        staged_path = self.resolve_staged_uri(
            staged_storage_uri,
            scope=scope,
        )

        if not staged_path.is_file():
            raise FileNotFoundError(
                "索引候选文件已经不存在，请重新上传文档。"
            )

        if (
            self._calculate_hash(staged_path)
            != expected_content_hash
        ):
            raise ManagedDocumentPathError(
                "暂存候选文件内容哈希不匹配"
            )

        return staged_path

    def resolve_staged_uri(
        self,
        storage_uri: str,
        *,
        scope: RetrievalScope,
    ) -> Path:
        path = self._resolve_managed_uri(storage_uri)
        expected_directory = (
            self._scope_directory(scope)
            / ".staging"
        ).resolve()

        if path.parent != expected_directory:
            raise ManagedDocumentPathError(
                "候选文件不在当前知识库的暂存目录中"
            )

        return path

    def resolve_target_uri(
        self,
        storage_uri: str,
        *,
        scope: RetrievalScope,
    ) -> Path:
        path = self._resolve_managed_uri(storage_uri)

        if path.parent != self._scope_directory(scope):
            raise ManagedDocumentPathError(
                "正式文件不在当前知识库的存储目录中"
            )

        return path

    def _scope_directory(
        self,
        scope: RetrievalScope,
    ) -> Path:
        directory = (
            self._storage_dir
            / str(scope.tenant_id)
            / str(scope.knowledge_base_id)
        ).resolve()

        try:
            directory.relative_to(self._storage_dir)

        except ValueError as exc:
            raise ManagedDocumentPathError(
                "知识库存储路径不在受管目录中"
            ) from exc

        return directory

    def _resolve_managed_uri(
        self,
        storage_uri: str,
    ) -> Path:
        parsed = urlparse(storage_uri)

        if (
            parsed.scheme.lower() != "file"
            or parsed.netloc not in {"", "localhost"}
        ):
            raise ManagedDocumentPathError(
                "只支持受管目录中的本地文件"
            )

        decoded_path = unquote(parsed.path)

        if (
            operating_system == "nt"
            and len(decoded_path) >= 3
            and decoded_path[0] == "/"
            and decoded_path[2] == ":"
        ):
            decoded_path = decoded_path[1:]

        path = Path(decoded_path).resolve()

        try:
            path.relative_to(self._storage_dir)

        except ValueError as exc:
            raise ManagedDocumentPathError(
                "文件路径不在受管存储目录中"
            ) from exc

        return path

    @staticmethod
    def _calculate_hash(path: Path) -> str:
        hasher = hashlib.sha256()

        with path.open("rb") as file:
            while block := file.read(READ_BLOCK_SIZE):
                hasher.update(block)

        return hasher.hexdigest()

    @staticmethod
    def _remove_empty_directory(directory: Path) -> None:
        try:
            directory.rmdir()

        except OSError:
            pass
