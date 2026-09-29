from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from .db.models import KnowledgeDocument
from .pgvector_indexing_service import (
    PgVectorIndexingService,
)
from .security.retrieval_scope import RetrievalScope


SUPPORTED_SUFFIXES = {
    ".pdf",
    ".docx",
    ".txt",
    ".md",
}

READ_BLOCK_SIZE = 1024 * 1024

WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *{
        f"COM{index}"
        for index in range(1, 10)
    },
    *{
        f"LPT{index}"
        for index in range(1, 10)
    },
}


class InvalidUpload(ValueError):
    """上传文件不符合要求。"""


class UnsupportedUploadType(InvalidUpload):
    """上传文件类型不受支持。"""


class UploadTooLarge(InvalidUpload):
    """上传文件超过大小限制。"""


@dataclass(frozen=True, slots=True)
class DocumentUploadResult:
    document: KnowledgeDocument
    indexed_chunk_count: int
    skipped: bool


class DocumentUploadService:
    """安全保存上传文件并写入 pgvector。"""

    def __init__(
        self,
        *,
        session: Session,
        indexing_service: PgVectorIndexingService,
        storage_dir: str | Path,
        max_upload_bytes: int,
    ) -> None:
        if max_upload_bytes < 1:
            raise ValueError(
                "max_upload_bytes 必须是正整数"
            )

        self._session = session
        self._indexing_service = indexing_service
        self._storage_dir = (
            Path(storage_dir)
            .expanduser()
            .resolve()
        )
        self._max_upload_bytes = max_upload_bytes

    def upload(
        self,
        *,
        file_name: str,
        source: BinaryIO,
        scope: RetrievalScope,
        created_by_user_id: UUID,
    ) -> DocumentUploadResult:
        safe_file_name = self._validate_file_name(
            file_name
        )

        directory = (
            self._storage_dir
            / str(scope.tenant_id)
            / str(scope.knowledge_base_id)
        ).resolve()

        directory.mkdir(
            parents=True,
            exist_ok=True,
        )

        target_path = (
            directory / safe_file_name
        ).resolve()

        if target_path.parent != directory:
            raise InvalidUpload(
                "上传文件路径不合法"
            )

        temporary_path = (
            directory
            / f".upload-{uuid4().hex}.tmp"
        )

        backup_path = (
            directory
            / f".backup-{uuid4().hex}.tmp"
        )

        self._write_temporary_file(
            source=source,
            temporary_path=temporary_path,
        )

        backup_created = False
        target_installed = False

        try:
            if target_path.exists():
                target_path.replace(backup_path)
                backup_created = True

            temporary_path.replace(target_path)
            target_installed = True

            indexed_chunk_count = (
                self._indexing_service.index_file(
                    file_path=target_path,
                    scope=scope,
                    created_by_user_id=(
                        created_by_user_id
                    ),
                )
            )

        except Exception:
            if target_installed:
                target_path.unlink(
                    missing_ok=True
                )

            if backup_created:
                backup_path.replace(target_path)

            raise

        else:
            backup_path.unlink(
                missing_ok=True
            )

        finally:
            temporary_path.unlink(
                missing_ok=True
            )

        document = self._session.scalar(
            select(KnowledgeDocument).where(
                KnowledgeDocument.tenant_id
                == scope.tenant_id,
                KnowledgeDocument.knowledge_base_id
                == scope.knowledge_base_id,
                KnowledgeDocument.storage_uri
                == target_path.as_uri(),
            )
        )

        if document is None:
            raise RuntimeError(
                "入库完成但没有找到文档记录"
            )

        return DocumentUploadResult(
            document=document,
            indexed_chunk_count=(
                indexed_chunk_count
            ),
            skipped=indexed_chunk_count == 0,
        )

    def _write_temporary_file(
        self,
        *,
        source: BinaryIO,
        temporary_path: Path,
    ) -> None:
        total_bytes = 0

        try:
            with temporary_path.open("xb") as output:
                while True:
                    block = source.read(
                        READ_BLOCK_SIZE
                    )

                    if not block:
                        break

                    if not isinstance(
                        block,
                        (
                            bytes,
                            bytearray,
                            memoryview,
                        ),
                    ):
                        raise InvalidUpload(
                            "上传内容必须是二进制文件"
                        )

                    total_bytes += len(block)

                    if (
                        total_bytes
                        > self._max_upload_bytes
                    ):
                        raise UploadTooLarge(
                            "上传文件超过允许的最大大小"
                        )

                    output.write(block)

            if total_bytes == 0:
                raise InvalidUpload(
                    "上传文件不能为空"
                )

        except Exception:
            temporary_path.unlink(
                missing_ok=True
            )
            raise

    @staticmethod
    def _validate_file_name(
        file_name: str,
    ) -> str:
        normalized = file_name.strip()

        if not normalized:
            raise InvalidUpload(
                "上传文件缺少文件名"
            )

        if len(normalized) > 255:
            raise InvalidUpload(
                "上传文件名不能超过255个字符"
            )

        if normalized in {".", ".."}:
            raise InvalidUpload(
                "上传文件名不合法"
            )

        if (
            "/" in normalized
            or "\\" in normalized
        ):
            raise InvalidUpload(
                "上传文件名不能包含路径分隔符"
            )

        if normalized.endswith("."):
            raise InvalidUpload(
                "上传文件名不能以点结尾"
            )

        if any(
            ord(character) < 32
            or character in '<>:"|?*'
            for character in normalized
        ):
            raise InvalidUpload(
                "上传文件名包含非法字符"
            )

        device_name = (
            normalized
            .split(".", 1)[0]
            .upper()
        )

        if device_name in WINDOWS_RESERVED_NAMES:
            raise InvalidUpload(
                "上传文件名使用了系统保留名称"
            )

        suffix = Path(normalized).suffix.lower()

        if suffix not in SUPPORTED_SUFFIXES:
            raise UnsupportedUploadType(
                "暂不支持该文件类型："
                f"{suffix or '无扩展名'}"
            )

        return normalized