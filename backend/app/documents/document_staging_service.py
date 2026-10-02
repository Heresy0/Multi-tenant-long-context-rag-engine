import hashlib
import mimetypes

from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO
from uuid import uuid4

from ..security.retrieval_scope import RetrievalScope


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
class StagedDocument:
    """已经安全写入暂存目录的候选文档。"""

    file_name: str
    staged_path: Path
    target_path: Path
    mime_type: str
    content_hash: str
    size_bytes: int


class DocumentStagingService:
    """校验并暂存上传文件，不覆盖正式文件。"""

    def __init__(
        self,
        *,
        storage_dir: str | Path,
        max_upload_bytes: int,
    ) -> None:
        if max_upload_bytes < 1:
            raise ValueError(
                "max_upload_bytes 必须是正整数"
            )

        self._storage_dir = (
            Path(storage_dir)
            .expanduser()
            .resolve()
        )
        self._max_upload_bytes = max_upload_bytes

    def stage(
        self,
        *,
        file_name: str,
        source: BinaryIO,
        scope: RetrievalScope,
    ) -> StagedDocument:
        """把上传内容写入知识库范围内的暂存目录。"""
        safe_file_name = self.validate_file_name(
            file_name
        )

        directory = (
            self._storage_dir
            / str(scope.tenant_id)
            / str(scope.knowledge_base_id)
        ).resolve()

        self._require_managed_path(directory)

        staging_directory = (
            directory / ".staging"
        ).resolve()

        self._require_managed_path(
            staging_directory
        )

        staging_directory.mkdir(
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

        suffix = Path(safe_file_name).suffix.lower()

        # 保留原扩展名，让后续解析器能够识别文件类型。
        staged_path = (
            staging_directory
            / f"{uuid4().hex}{suffix}"
        ).resolve()

        if staged_path.parent != staging_directory:
            raise InvalidUpload(
                "暂存文件路径不合法"
            )

        content_hash = hashlib.sha256()
        total_bytes = 0

        try:
            with staged_path.open("xb") as output:
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
                    content_hash.update(block)

            if total_bytes == 0:
                raise InvalidUpload(
                    "上传文件不能为空"
                )

        except Exception:
            staged_path.unlink(
                missing_ok=True
            )
            self._remove_empty_staging_directory(
                staging_directory
            )
            raise

        return StagedDocument(
            file_name=safe_file_name,
            staged_path=staged_path,
            target_path=target_path,
            mime_type=(
                mimetypes.guess_type(
                    safe_file_name
                )[0]
                or "application/octet-stream"
            ),
            content_hash=content_hash.hexdigest(),
            size_bytes=total_bytes,
        )

    def discard(
        self,
        staged_document: StagedDocument,
    ) -> None:
        """安全删除候选文件，重复调用也不会报错。"""
        staged_path = (
            staged_document
            .staged_path
            .resolve()
        )

        self._require_managed_staging_path(
            staged_path
        )

        staged_path.unlink(
            missing_ok=True
        )

        self._remove_empty_staging_directory(
            staged_path.parent
        )

    @staticmethod
    def validate_file_name(
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

    def _require_managed_path(
        self,
        path: Path,
    ) -> None:
        try:
            path.relative_to(
                self._storage_dir
            )

        except ValueError as exc:
            raise InvalidUpload(
                "文件路径不在受管存储目录中"
            ) from exc

    def _require_managed_staging_path(
        self,
        path: Path,
    ) -> None:
        self._require_managed_path(path)

        if path.parent.name != ".staging":
            raise InvalidUpload(
                "只能清理暂存目录中的文件"
            )

    @staticmethod
    def _remove_empty_staging_directory(
        directory: Path,
    ) -> None:
        try:
            directory.rmdir()

        except OSError:
            # 目录非空或已经被删除时不需要处理。
            pass
