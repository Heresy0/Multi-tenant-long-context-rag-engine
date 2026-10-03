"""Persist paired turns; serialize requests without holding a DB lock during LLM calls."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import UUID, uuid4

from sqlalchemy import delete, or_, select, update
from sqlalchemy.orm import Session

from ..db.conversation_models import Conversation, ConversationTurn
from ..security.principal import Principal
from ..security.retrieval_scope import RetrievalScope
from .answer_models import AnswerResult

HISTORY_TURNS = 6
LEASE_SECONDS = 240


class ConversationNotFound(Exception):
    pass


class ConversationBusy(Exception):
    pass


class ConversationStore:
    def __init__(self, session: Session, principal: Principal, scope: RetrievalScope):
        if principal.tenant_id != scope.tenant_id:
            raise ConversationNotFound("对话不存在。")
        self.session, self.principal, self.scope = session, principal, scope

    def _owner(self):
        return (
            Conversation.tenant_id == self.scope.tenant_id,
            Conversation.user_id == self.principal.user_id,
            Conversation.knowledge_base_id == self.scope.knowledge_base_id,
        )

    def get(self, conversation_id: UUID) -> Conversation:
        row = self.session.scalar(select(Conversation).where(*self._owner(), Conversation.id == conversation_id)
                                  .execution_options(populate_existing=True))
        if row is None:
            raise ConversationNotFound("对话不存在或不属于当前用户和知识库。")
        return row

    def create(self, *, commit=True) -> Conversation:
        row = Conversation(tenant_id=self.scope.tenant_id, user_id=self.principal.user_id, knowledge_base_id=self.scope.knowledge_base_id)
        self.session.add(row)
        self.session.flush()
        if commit:
            self.session.commit()
        self.session.refresh(row)
        return row

    def list(self, limit=20, offset=0):
        return list(self.session.scalars(select(Conversation).where(*self._owner()).order_by(Conversation.updated_at.desc(), Conversation.id.desc()).limit(limit).offset(offset)))

    def _turn_is_readable(self, turn):
        # 全范围模式保存整个候选范围，保守覆盖引用遗漏及追问的间接依赖。
        ids = turn.authorized_knowledge_base_ids
        if not ids and turn.knowledge_base_id is not None:
            ids = [str(turn.knowledge_base_id)]  # 迁移前的单库历史。
        try:
            return bool(ids) and {UUID(value) for value in ids} <= self.scope.knowledge_base_ids
        except (TypeError, ValueError):
            return False

    def safe_title(self, row):
        first = self.session.scalar(select(ConversationTurn).where(
            ConversationTurn.tenant_id == self.scope.tenant_id,
            ConversationTurn.conversation_id == row.id,
            ConversationTurn.turn_index == 1,
        ))
        return row.title if first is None or self._turn_is_readable(first) else "历史权限已变更"

    def turns(self, conversation_id: UUID, *, limit=100, before=None, for_context=False):
        self.get(conversation_id)
        statement = select(ConversationTurn).where(
            ConversationTurn.tenant_id == self.scope.tenant_id,
            ConversationTurn.knowledge_base_id == self.scope.knowledge_base_id,
            ConversationTurn.conversation_id == conversation_id,
        )
        if before is not None:
            statement = statement.where(ConversationTurn.turn_index < before)
        rows = list(self.session.scalars(statement.order_by(ConversationTurn.turn_index.desc()).limit(limit)))
        rows.reverse()
        if for_context:
            # 失效轮次之前的上下文也不再沿用，避免跨越权限变化继续旧话题。
            start = 0
            for i, turn in enumerate(rows):
                if not self._turn_is_readable(turn):
                    start = i + 1
            return rows[start:]
        return [turn if self._turn_is_readable(turn) else SimpleNamespace(
            turn_index=turn.turn_index, question="历史内容已隐藏：访问权限发生变化。",
            retrieval_question="", created_at=turn.created_at,
            result_json=AnswerResult(answer="该轮历史不再可访问，请重新提问。", answerable=False,
                                     citations=[], refusal_reason="知识库访问权限已变更。").model_dump(mode="json"),
        ) for turn in rows]

    def acquire(self, conversation_id: UUID) -> str:
        """原子抢占有超时的租约，模型调用期间不持有数据库行锁。"""
        self.get(conversation_id)
        token = uuid4().hex
        now = datetime.now(timezone.utc)
        changed = self.session.execute(update(Conversation).where(
            *self._owner(), Conversation.id == conversation_id,
            or_(Conversation.lease_token.is_(None), Conversation.lease_expires_at <= now),
        ).values(lease_token=token, lease_expires_at=now + timedelta(seconds=LEASE_SECONDS)).execution_options(synchronize_session=False)).rowcount
        if changed != 1:
            self.session.rollback()
            raise ConversationBusy("这段对话正在回答另一个问题，请稍后重试。")
        self.session.commit()
        return token

    def append(self, conversation_id: UUID, token: str, question: str, retrieval_question: str, result: AnswerResult, *, commit=True):
        """同时写入问题和回答；过期或被替换的租约不能提交结果。"""
        row = self.get(conversation_id)
        index = row.turn_count + 1
        changed = self.session.execute(update(Conversation).where(
            *self._owner(), Conversation.id == conversation_id,
            Conversation.lease_token == token,
            Conversation.lease_expires_at > datetime.now(timezone.utc),
            Conversation.turn_count == index - 1,
        ).values(turn_count=index, title=question[:200] if index == 1 else row.title,
                 lease_token=None, lease_expires_at=None, updated_at=datetime.now(timezone.utc))
            .execution_options(synchronize_session=False)).rowcount
        if changed != 1:
            self.session.rollback()
            raise ConversationBusy("对话已改变或处理超时，请重新加载后重试。")
        self.session.add(ConversationTurn(
            tenant_id=self.scope.tenant_id, knowledge_base_id=self.scope.knowledge_base_id,
            conversation_id=conversation_id, turn_index=index, question=question,
            retrieval_question=retrieval_question, result_json=result.model_dump(mode="json"),
            authorized_knowledge_base_ids=sorted(map(str, self.scope.knowledge_base_ids)),
        ))
        self.session.flush()
        if commit:
            self.session.commit()

    def release(self, conversation_id: UUID, token: str):
        # Roll back failed retrieval/storage transactions before releasing only our lease.
        self.session.rollback()
        self.session.execute(update(Conversation).where(*self._owner(), Conversation.id == conversation_id,
            Conversation.lease_token == token).values(lease_token=None, lease_expires_at=None)
            .execution_options(synchronize_session=False))
        self.session.commit()

    def delete(self, conversation_id: UUID, *, commit=True):
        self.get(conversation_id)
        changed = self.session.execute(delete(Conversation).where(*self._owner(), Conversation.id == conversation_id,
            or_(Conversation.lease_token.is_(None), Conversation.lease_expires_at <= datetime.now(timezone.utc)))
            .execution_options(synchronize_session=False)).rowcount
        if changed != 1:
            self.session.rollback()
            raise ConversationBusy("对话正在生成答案，暂时不能删除。")
        if commit:
            self.session.commit()
