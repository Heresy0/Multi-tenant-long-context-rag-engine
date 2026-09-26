from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker


def create_database_engine(
    database_url: str,
) -> Engine:
    """创建数据库连接引擎。"""
    return create_engine(
        database_url,
        pool_pre_ping=True,
    )


def create_session_factory(
    engine: Engine,
) -> sessionmaker[Session]:
    """创建数据库会话工厂。"""
    return sessionmaker(
        bind=engine,
        class_=Session,
        expire_on_commit=False,
    )