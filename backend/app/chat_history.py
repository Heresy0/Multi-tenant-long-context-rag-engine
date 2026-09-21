import sqlite3
from pathlib import Path
from contextlib import closing




class ChatHistoryRepository:
    """管理用户可见的完整聊天记录。"""

    VALID_ROLES = {
        "user",
        "assistant",
        "tool",
    }

    def __init__(self,database_path: Path):
        self.database_path = database_path


    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.database_path,
            timeout = 10,
        )
        connection.row_factory = sqlite3.Row
        # SQLite 默认不会强制执行外键约束
        connection.execute(
            "PRAGMA foreign_keys = ON"
        )
        return connection


    def setup(self):
        """初始化会话表和消息表。"""
        self.database_path.parent.mkdir(
            parents = True,
            exist_ok = True,
        )
        with closing(self.connect()) as connection:
            # 提升并发读取能力
            connection.execute(
                "PRAGMA journal_mode = WAL"
            )

            connection.execute("""
                CREATE TABLE IF NOT EXISTS conversations(
                    id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL
                        DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL
                        DEFAULT CURRENT_TIMESTAMP
                )                
            """)

            connection.execute("""
                CREATE TABLE IF NOT EXISTS chat_messages(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    conversation_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL
                        DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (conversation_id)
                        REFERENCES conversations(id)
                        ON DELETE CASCADE
                )
            """)

            connection.execute("""
                CREATE INDEX IF NOT EXISTS
                    idx_chat_messages_conversation
                ON chat_messages(
                    conversation_id,
                    id
                )
            """)

            connection.commit()


    def create_conversation(self,conversation_id: str,):
        with sqlite3.connect(
            self.database_path
        ) as connection:
            connection.execute("" \
                """
                INSERT OR IGNORE INTO conversations(id)
                VALUES(?)
                """,
                (conversation_id,),
            )


    def add_message(
            self,
            conversation_id: str,
            role: str,
            content: str,
    ):
        """创建会话并保存一条消息。"""
        if role not in self.VALID_ROLES:
            raise ValueError(
                f"不支持的消息角色：{role}"
            )

        if not content:
            raise ValueError("消息内容不能为空")

        with closing(self.connect()) as connection:
            # 其中任意操作失败，整个事务都会回滚
            with connection:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO conversations(id)
                    VALUES(?)
                    """,
                    (conversation_id,),
                )

                connection.execute(
                    """
                    INSERT INTO chat_messages(
                        conversation_id,
                        role,
                        content
                    )
                    VALUES(?,?,?)
                    """,
                    (
                        conversation_id,
                        role,
                        content,
                    ),
                )

                connection.execute(
                    """
                    UPDATE conversations
                    SET updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (conversation_id,),
                )
            




    def list_messages(
            self,
            conversation_id: str,
    ) -> list[dict]:
        """按产生顺序查询指定会话的完整消息。"""
        with closing(self.connect())as connection:
            rows = connection.execute(
                """
                SELECT
                    id,
                    role,
                    content,
                    created_at
                FROM chat_messages
                WHERE conversation_id = ?
                ORDER BY id ASC
                """,
                (conversation_id,),
            ).fetchall()

        return [dict(row) for row in rows]


    def list_conversations(self) -> list[dict]:
        """查询全部会话，最近更新的排在前面。"""
        with closing(self.connect()) as connection:
            rows = connection.execute(
                """
                SELECT
                    id,
                    created_at,
                    updated_at
                    FROM conversations
                    ORDER BY updated_at DESC
                """
            ).fetchall()

        return [dict(row) for row in rows]
        