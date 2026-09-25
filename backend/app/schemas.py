from pydantic import BaseModel, Field
from .answer_models import AnswerResult

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
    question: str = Field(
        min_length=1,
        max_length=1000,
    )


class KnowledgeQuestionResponse(AnswerResult):
    pass
