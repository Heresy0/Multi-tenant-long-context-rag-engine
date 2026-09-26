from collections.abc import Iterator

from fastapi import Request
from sqlalchemy.orm import Session, sessionmaker


def get_database_session(
    request: Request,
) -> Iterator[Session]:
    """为单次 HTTP 请求提供数据库会话。"""
    session_factory: sessionmaker[Session] = (
        request.app.state.database_session_factory
    )

    session = session_factory()

    try:
        yield session

    except Exception:
        session.rollback()
        raise

    finally:
        session.close()