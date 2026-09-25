from pydantic import BaseModel, Field


class AnswerClaim(BaseModel):
    text: str = Field(
        min_length=1,
        description=(
        "依据知识库资料生成的一条独立结论，"
        "文本中不要自行编造引用编号。"
        ),
    )
    citations: list[str] = Field(
        default_factory=list,
        description=(
            "支持该结论的资料编号数组。"
            "可回答时不能为空，例如："
            "['资料1'] 或 ['资料1', '资料2']。"
            "编号不能带方括号。"
        ),
    )
    

class AnswerDraft(BaseModel):
    answerable: bool
    claims: list[AnswerClaim] = Field(
        default_factory=list
    )
    refusal_reason: str | None = None


class Citation(BaseModel):
    citation_id: str
    document_name: str
    section_path: str
    source: str
    chunk_id: str | None = None


class AnswerTimings(BaseModel):
    hybrid_retrieval_ms: float | None = None
    rerank_ms: float | None = None
    search_total_ms: float
    context_build_ms: float
    generation_ms: float | None = None
    validation_ms: float | None = None
    render_ms: float | None = None
    total_ms: float


class AnswerResult(BaseModel):
    answer: str
    answerable: bool
    citations: list[Citation]
    refusal_reason: str | None = None
    timings: AnswerTimings | None = None
