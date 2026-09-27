import argparse
import sys
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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="将本地文件写入指定的 pgvector 知识库。",
    )
    parser.add_argument(
        "--file",
        type=Path,
        required=True,
        help="需要入库的本地文件路径。",
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
    return parser.parse_args()


def run() -> None:
    args = parse_args()
    file_path = args.file.expanduser().resolve()

    if not file_path.is_file():
        raise SystemExit(
            f"文件不存在或不是普通文件：{file_path}"
        )

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

            chunk_count = indexing_service.index_file(
                file_path=file_path,
                scope=scope,
                created_by_user_id=args.user_id,
            )

            if chunk_count == 0:
                print(
                    "文件内容和索引配置没有变化，"
                    "跳过重复入库。"
                )
            else:
                print("入库完成：")
                print(f"file={file_path}")
                print(f"chunks={chunk_count}")
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
