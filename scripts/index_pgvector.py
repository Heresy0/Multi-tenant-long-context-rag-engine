import argparse
import sys
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import select  # noqa: E402
from openai import OpenAIError  # noqa: E402
from sqlalchemy.exc import SQLAlchemyError  # noqa: E402

from backend.app.config import Settings  # noqa: E402
from backend.app.db.models import User  # noqa: E402
from backend.app.db.session import (  # noqa: E402
    create_database_engine,
    create_session_factory,
)
from backend.app.security.authorization import (  # noqa: E402
    AuthorizationDenied,
    AuthorizationService,
)
from backend.app.security.principal import (  # noqa: E402
    Principal,
)
from backend.app.pgvector_indexing_service import (  # noqa: E402
    PgVectorIndexingService,
)
from backend.app.security.retrieval_scope import (  # noqa: E402
    RetrievalScope,
)


SUPPORTED_SUFFIXES = {
    ".pdf",
    ".docx",
    ".txt",
    ".md",
}


@dataclass(frozen=True, slots=True)
class IndexingFailure:
    file_path: Path
    error: Exception


@dataclass(frozen=True, slots=True)
class IndexingSummary:
    discovered_count: int
    indexed_count: int
    skipped_count: int
    chunk_count: int
    failures: tuple[IndexingFailure, ...]

    @property
    def failed_count(self) -> int:
        return len(self.failures)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "将本地文件或目录批量写入指定的 "
            "pgvector 知识库。"
        ),
    )
    input_group = parser.add_mutually_exclusive_group(
        required=True,
    )
    input_group.add_argument(
        "--file",
        type=Path,
        help="需要入库的单个本地文件路径。",
    )
    input_group.add_argument(
        "--directory",
        type=Path,
        help="需要批量入库的本地目录。",
    )
    parser.add_argument(
        "--recursive",
        action="store_true",
        help="递归扫描目录中的子目录。",
    )
    parser.add_argument(
        "--tenant-id",
        type=UUID,
        required=True,
        help="目标租户 UUID。",
    )
    parser.add_argument(
        "--knowledge-base-id",
        type=UUID,
        required=True,
        help="目标知识库 UUID。",
    )
    parser.add_argument(
        "--user-id",
        type=UUID,
        required=True,
        help="执行入库操作的用户 UUID。",
    )
    args = parser.parse_args()

    if args.recursive and args.directory is None:
        parser.error("--recursive 只能和 --directory 一起使用")

    return args


def discover_files(
    *,
    file_path: Path | None,
    directory_path: Path | None,
    recursive: bool,
) -> list[Path]:
    """校验输入并按稳定顺序返回支持的文档。"""
    if file_path is not None:
        path = file_path.expanduser().resolve()

        if not path.is_file():
            raise SystemExit(
                f"文件不存在或不是普通文件：{path}"
            )

        if path.suffix.lower() not in SUPPORTED_SUFFIXES:
            raise SystemExit(
                f"暂不支持该文件类型：{path.suffix.lower()}"
            )

        return [path]

    if directory_path is None:
        raise RuntimeError("没有指定文件或目录")

    directory = directory_path.expanduser().resolve()

    if not directory.is_dir():
        raise SystemExit(
            f"目录不存在或不是目录：{directory}"
        )

    candidates = (
        directory.rglob("*")
        if recursive
        else directory.glob("*")
    )
    files = sorted(
        (
            path.resolve()
            for path in candidates
            if path.is_file()
            and path.suffix.lower() in SUPPORTED_SUFFIXES
        ),
        key=lambda path: str(path).casefold(),
    )

    if not files:
        mode = "及其子目录" if recursive else ""
        raise SystemExit(
            f"目录{mode}中没有支持的文档：{directory}"
        )

    return files


def index_paths(
    *,
    indexing_service: PgVectorIndexingService,
    file_paths: list[Path],
    scope: RetrievalScope,
    created_by_user_id: UUID,
    continue_on_error: bool,
) -> IndexingSummary:
    """依次入库；目录模式允许单文件失败后继续。"""
    indexed_count = 0
    skipped_count = 0
    chunk_count = 0
    failures: list[IndexingFailure] = []

    for position, file_path in enumerate(
        file_paths,
        start=1,
    ):
        print(
            f"[{position}/{len(file_paths)}] "
            f"处理：{file_path}"
        )

        try:
            written_count = indexing_service.index_file(
                file_path=file_path,
                scope=scope,
                created_by_user_id=created_by_user_id,
            )

        except Exception as exc:
            if not continue_on_error:
                raise

            failures.append(
                IndexingFailure(
                    file_path=file_path,
                    error=exc,
                )
            )
            print(
                f"  失败：{type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            continue

        if written_count == 0:
            skipped_count += 1
            print("  跳过：内容和索引配置没有变化。")
        else:
            indexed_count += 1
            chunk_count += written_count
            print(f"  完成：写入 {written_count} 个分块。")

    return IndexingSummary(
        discovered_count=len(file_paths),
        indexed_count=indexed_count,
        skipped_count=skipped_count,
        chunk_count=chunk_count,
        failures=tuple(failures),
    )


def print_summary(
    summary: IndexingSummary,
    *,
    knowledge_base_id: UUID,
) -> None:
    print("批量入库汇总：")
    print(f"发现文件：{summary.discovered_count}")
    print(f"新增或更新：{summary.indexed_count}")
    print(f"内容未变化：{summary.skipped_count}")
    print(f"失败：{summary.failed_count}")
    print(f"写入分块：{summary.chunk_count}")
    print(f"knowledge_base_id={knowledge_base_id}")

    if summary.failures:
        print("失败文件：", file=sys.stderr)

        for failure in summary.failures:
            print(
                f"- {failure.file_path}: "
                f"{type(failure.error).__name__}: "
                f"{failure.error}",
                file=sys.stderr,
            )


def run() -> None:
    args = parse_args()
    file_paths = discover_files(
        file_path=args.file,
        directory_path=args.directory,
        recursive=args.recursive,
    )
    directory_mode = args.directory is not None

    settings = Settings()
    engine = create_database_engine(
        settings.database_url
    )
    session_factory = create_session_factory(engine)

    try:
        with session_factory() as session:
            user = session.scalar(
                select(User).where(
                    User.id == args.user_id,
                    User.tenant_id == args.tenant_id,
                )
            )

            if user is None:
                raise SystemExit(
                    "指定用户不存在，或者不属于指定租户。"
                )

            principal = Principal(
                user_id=user.id,
                tenant_id=user.tenant_id,
                external_subject=user.external_subject,
            )

            authorization = AuthorizationService(session)

            authorization.require_permission(
                principal=principal,
                knowledge_base_id=(
                    args.knowledge_base_id
                ),
                required_permission="editor",
            )

            print("入库前置校验通过，开始生成向量……")

            if directory_mode:
                print(f"发现 {len(file_paths)} 个支持的文档。")

            scope = RetrievalScope(
                tenant_id=args.tenant_id,
                knowledge_base_id=(
                    args.knowledge_base_id
                ),
            )

            indexing_service = PgVectorIndexingService(
                session=session,
                settings=settings,
            )

            summary = index_paths(
                indexing_service=indexing_service,
                file_paths=file_paths,
                scope=scope,
                created_by_user_id=args.user_id,
                continue_on_error=directory_mode,
            )

            if directory_mode:
                print_summary(
                    summary,
                    knowledge_base_id=(
                        args.knowledge_base_id
                    ),
                )

                if summary.failed_count:
                    raise SystemExit(1)

            elif summary.skipped_count:
                print(
                    "文件内容和索引配置没有变化，"
                    "跳过重复入库。"
                )
            else:
                print("入库完成：")
                print(f"file={file_paths[0]}")
                print(f"chunks={summary.chunk_count}")
                print(
                    "knowledge_base_id="
                    f"{args.knowledge_base_id}"
                )

    finally:
        engine.dispose()


def main() -> None:
    try:
        run()

    except AuthorizationDenied:
        raise SystemExit(
            "权限校验失败：用户对目标知识库"
            "至少需要 editor 权限。"
        ) from None

    except SQLAlchemyError:
        raise SystemExit(
            "数据库连接或操作失败。"
            "请确认 PostgreSQL 已启动，"
            "并检查 DATABASE_URL。"
        ) from None

    except OpenAIError as exc:
        raise SystemExit(
            f"嵌入服务调用失败：{exc}"
        ) from None

    except (OSError, ValueError, RuntimeError) as exc:
        raise SystemExit(
            f"入库失败：{exc}"
        ) from None


if __name__ == "__main__":
    main()
