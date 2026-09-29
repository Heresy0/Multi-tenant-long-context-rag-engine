import argparse
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))


from backend.app.config import required_env  # noqa: E402
from backend.app.db.bootstrap import (  # noqa: E402
    provision_department_manager,
)
from backend.app.db.session import (  # noqa: E402
    create_database_engine,
    create_session_factory,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="开通已有企业的部门负责人和部门知识库。",
    )
    parser.add_argument("--tenant-name", required=True)
    parser.add_argument("--department-name", required=True)
    parser.add_argument("--subject", required=True)
    parser.add_argument("--user-name", required=True)
    parser.add_argument("--user-email")
    parser.add_argument(
        "--company-kb-name",
        default="公司公共知识库",
    )
    parser.add_argument("--department-kb-name")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    engine = create_database_engine(
        required_env("DATABASE_URL")
    )
    session_factory = create_session_factory(engine)

    try:
        with session_factory() as session:
            result = provision_department_manager(
                session,
                tenant_name=args.tenant_name,
                department_name=args.department_name,
                external_subject=args.subject,
                user_name=args.user_name,
                user_email=args.user_email,
                company_knowledge_base_name=(
                    args.company_kb_name
                ),
                department_knowledge_base_name=(
                    args.department_kb_name
                ),
            )

    finally:
        engine.dispose()

    print("部门负责人开通完成：")
    print(f"tenant_id={result.tenant_id}")
    print(f"user_id={result.user_id}")
    print(f"department_id={result.department_id}")
    print(
        "company_knowledge_base_id="
        f"{result.company_knowledge_base_id}"
    )
    print(
        "department_knowledge_base_id="
        f"{result.department_knowledge_base_id}"
    )


if __name__ == "__main__":
    main()
