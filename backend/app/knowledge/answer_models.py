from uuid import UUID
from typing import Literal

from pydantic import BaseModel, Field


class CalculationOperand(BaseModel):
    source: str = Field(description="问题，或引用编号如资料1；输入必须来自该来源")
    quote: str = Field(min_length=1, max_length=300, description="含输入值和单位的连续原文摘录")
    value: str = Field(min_length=1, max_length=40, description="数值字符串或HH:MM:SS起始时刻")
    unit: Literal["时刻", "秒", "分钟", "小时", "元"]


class CalculationRequest(BaseModel):
    operation: Literal["time_add", "money_sum", "money_difference"]
    inputs: list[CalculationOperand] = Field(min_length=2, max_length=20)
    result: str = Field(min_length=1, max_length=50, description="模型提出的结果；程序会独立复算，不信任此值")


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
    calculation: CalculationRequest | None = Field(default=None,
        description="仅用于受限的时间累加或金额求和/差额；直接事实为null。需逐个提供输入来源。")


class AnswerDraft(BaseModel):
    answerable: bool
    claims: list[AnswerClaim] = Field(
        default_factory=list
    )
    refusal_reason: str | None = None


class Citation(BaseModel):
    citation_id: str
    document_id: UUID | None = None
    document_name: str
    section_path: str
    chunk_id: str | None = None
    knowledge_base_id: UUID | None = None
    knowledge_base_name: str | None = None


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
