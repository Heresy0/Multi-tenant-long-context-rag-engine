from uuid import UUID
from pydantic import BaseModel, Field
from .answer_models import AnswerResult
from datetime import datetime



class ChatRequest(BaseModel):
    message: str = Field(
        min_length=1,
        max_length=1000,
    )
    conversation_id: str | None = None


class ChatResponse(BaseModel):
    conversation_id: str
    answer: str


class KnowledgeQuestionRequest(BaseModel):
    knowledge_base_id: UUID
    question: str = Field(
        min_length=1,
        max_length=1000,
    )


class KnowledgeQuestionResponse(AnswerResult):
    pass


class KnowledgeBaseSummary(BaseModel):
    id: UUID
    name: str
    visibility: str
    permission: str


class KnowledgeBaseListResponse(BaseModel):
    items: list[KnowledgeBaseSummary]


class KnowledgeDocumentSummary(BaseModel):
    id: UUID
    file_name: str
    mime_type: str
    status: str
    version: int
    chunk_count: int
    created_at: datetime
    updated_at: datetime


class KnowledgeDocumentListResponse(BaseModel):
    items: list[KnowledgeDocumentSummary]


class DocumentIndexingJobSummary(BaseModel):
    id: UUID
    status: str
    target_version: int
    attempt_count: int
    max_attempts: int
    created_at: datetime
    updated_at: datetime


class DocumentIndexingJobDetail(
    DocumentIndexingJobSummary
):
    document_id: UUID
    indexed_chunk_count: int | None
    last_error: str | None
    available_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class DocumentIndexingJobListResponse(BaseModel):
    items: list[DocumentIndexingJobDetail]


class KnowledgeDocumentUploadResponse(BaseModel):
    document: KnowledgeDocumentSummary
    indexing_job: DocumentIndexingJobSummary


class AuditEventSummary(BaseModel):
    id: UUID
    actor_user_id: UUID | None
    knowledge_base_id: UUID | None
    action: str
    resource_type: str
    resource_id: str | None
    outcome: str
    request_id: str | None
    details: dict[str, object]
    occurred_at: datetime


class AuditEventListResponse(BaseModel):
    items: list[AuditEventSummary]
