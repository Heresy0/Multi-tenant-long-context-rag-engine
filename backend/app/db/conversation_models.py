"""Private, knowledge-base-scoped conversation storage."""
from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, DateTime, ForeignKeyConstraint, Index, Integer, JSON, String, Text, UniqueConstraint, Uuid, text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base
from .models import TimestampMixin


class Conversation(TimestampMixin, Base):
    __tablename__ = "conversations"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    user_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    knowledge_base_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    title: Mapped[str] = mapped_column(String(200), default="新对话", nullable=False)
    turn_count: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"), nullable=False)
    lease_token: Mapped[str | None] = mapped_column(String(32))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        ForeignKeyConstraint(["tenant_id", "user_id"], ["users.tenant_id", "users.id"], name="fk_conversations_user", ondelete="CASCADE"),
        ForeignKeyConstraint(["tenant_id", "knowledge_base_id"], ["knowledge_bases.tenant_id", "knowledge_bases.id"], name="fk_conversations_kb", ondelete="CASCADE"),
        UniqueConstraint("tenant_id", "knowledge_base_id", "id", name="uq_conversations_scope_id"),
        UniqueConstraint("tenant_id", "id", name="uq_conversations_tenant_id"),
        CheckConstraint("turn_count >= 0", name="ck_conversations_turn_count"),
        Index("ix_conversations_owner_updated", "tenant_id", "user_id", "knowledge_base_id", "updated_at"),
    )


class ConversationTurn(Base):
    __tablename__ = "conversation_turns"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    knowledge_base_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    conversation_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    turn_index: Mapped[int] = mapped_column(Integer, nullable=False)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    retrieval_question: Mapped[str] = mapped_column(Text, nullable=False)
    result_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    authorized_knowledge_base_ids: Mapped[list] = mapped_column(JSON, nullable=False, default=list, server_default=text("'[]'"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP"), nullable=False)

    __table_args__ = (
        ForeignKeyConstraint(["tenant_id", "knowledge_base_id", "conversation_id"], ["conversations.tenant_id", "conversations.knowledge_base_id", "conversations.id"], name="fk_conversation_turns_conversation", ondelete="CASCADE"),
        # 原三列外键遇到 NULL 会跳过检查；此外键仍保证跨库会话的租户隔离。
        ForeignKeyConstraint(["tenant_id", "conversation_id"], ["conversations.tenant_id", "conversations.id"], name="fk_conversation_turns_tenant_conversation", ondelete="CASCADE"),
        UniqueConstraint("conversation_id", "turn_index", name="uq_conversation_turns_position"),
        CheckConstraint("turn_index >= 1", name="ck_conversation_turns_index"),
    )
