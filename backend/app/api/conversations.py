"""Conversation access is private and re-authorized on every request."""
from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from ..audit_service import AuditService
from ..db.dependencies import get_database_session
from ..knowledge.answer_models import AnswerResult
from ..knowledge.conversations import ConversationBusy, ConversationNotFound, ConversationStore
from ..security.authorization import AuthorizationDenied, AuthorizationService
from ..security.dependencies import get_current_principal
from ..security.principal import Principal

router = APIRouter(tags=["conversations"])
SINGLE_PATH = "/api/knowledge-bases/{knowledge_base_id}/conversations"
ALL_PATH = "/api/conversations"


class ConversationSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    knowledge_base_id: UUID | None = None
    title: str
    turn_count: int
    created_at: datetime
    updated_at: datetime


class ConversationList(BaseModel):
    items: list[ConversationSummary]


class TurnSummary(BaseModel):
    turn_index: int
    question: str
    retrieval_question: str
    result: AnswerResult
    created_at: datetime


class ConversationDetail(ConversationSummary):
    turns: list[TurnSummary]
    # Fetch earlier history using ?before=<next_before>; latest 100 by default.
    next_before: int | None = None


def get_store(knowledge_base_id: UUID | None = None, principal: Principal = Depends(get_current_principal),
              session: Session = Depends(get_database_session)):
    try:
        scope = AuthorizationService(session).require_retrieval_scope(
            principal=principal, knowledge_base_id=knowledge_base_id,
        )
    except AuthorizationDenied as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return ConversationStore(session, principal, scope)


def checked_get(store, conversation_id):
    try:
        return store.get(conversation_id)
    except ConversationNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post(ALL_PATH, response_model=ConversationSummary, status_code=201)
@router.post(SINGLE_PATH, response_model=ConversationSummary, status_code=201)
def create_conversation(store: ConversationStore = Depends(get_store)):
    row = store.create(commit=False)
    AuditService(store.session).record(
        tenant_id=store.scope.tenant_id, actor_user_id=store.principal.user_id,
        knowledge_base_id=store.scope.knowledge_base_id, action="conversation.created",
        resource_type="conversation", resource_id=row.id, outcome="success",
    )
    return row


def summary(store, row):
    return ConversationSummary.model_validate(row).model_copy(update={"title": store.safe_title(row)})


@router.get(ALL_PATH, response_model=ConversationList)
@router.get(SINGLE_PATH, response_model=ConversationList)
def list_conversations(limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0),
                       store: ConversationStore = Depends(get_store)):
    return ConversationList(items=[summary(store, row) for row in store.list(limit, offset)])


@router.get(ALL_PATH + "/{conversation_id}", response_model=ConversationDetail)
@router.get(SINGLE_PATH + "/{conversation_id}", response_model=ConversationDetail)
def get_conversation(conversation_id: UUID, limit: int = Query(100, ge=1, le=100),
                     before: int | None = Query(None, ge=1), store: ConversationStore = Depends(get_store)):
    row = checked_get(store, conversation_id)
    try:
        turns = store.turns(conversation_id, limit=limit, before=before)
    except ConversationNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return ConversationDetail(
        **summary(store, row).model_dump(),
        turns=[TurnSummary(turn_index=t.turn_index, question=t.question,
                           retrieval_question=t.retrieval_question, result=AnswerResult.model_validate(t.result_json),
                           created_at=t.created_at) for t in turns],
        next_before=turns[0].turn_index if turns and turns[0].turn_index > 1 else None,
    )


@router.delete(ALL_PATH + "/{conversation_id}", status_code=204)
@router.delete(SINGLE_PATH + "/{conversation_id}", status_code=204)
def delete_conversation(conversation_id: UUID, store: ConversationStore = Depends(get_store)):
    checked_get(store, conversation_id)
    try:
        store.delete(conversation_id, commit=False)
    except ConversationBusy as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    AuditService(store.session).record(
        tenant_id=store.scope.tenant_id, actor_user_id=store.principal.user_id,
        knowledge_base_id=store.scope.knowledge_base_id, action="conversation.deleted",
        resource_type="conversation", resource_id=conversation_id, outcome="success",
    )
    return Response(status_code=204)
