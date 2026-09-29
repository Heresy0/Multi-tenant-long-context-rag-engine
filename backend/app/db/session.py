from sqlalchemy import Engine, create_engine, text
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


def verify_database_connection(
    engine: Engine,
) -> None:
    """确认应用启动时数据库可连接。"""
    with engine.connect() as connection:
        connection.execute(text("SELECT 1"))