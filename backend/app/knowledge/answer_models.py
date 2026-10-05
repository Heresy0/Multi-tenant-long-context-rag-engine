from uuid import UUID
from typing import Annotated, Literal

from pydantic import BaseModel, Field


class CalculationOperand(BaseModel):
    source: str = Field(description="问题，或引用编号如资料1；输入必须来自该来源")
    quote: str = Field(min_length=1, max_length=300, description="含输入值和单位的连续原文摘录")
    value: str = Field(min_length=1, max_length=40, description="数值字符串或HH:MM:SS起始时刻")
    unit: Literal["时刻", "秒", "分钟", "小时", "元"]


class CalculationRequest(BaseModel):
    operation: Literal["time_add", "money_sum", "money_difference"]
    inputs: list[CalculationOperand] = Field(min_length=2, max_length=20)
    result: str | None = Field(default=None, min_length=1, max_length=50,
                               description="可留null。若填写，仅作审计用的模型猜测；最终结果由程序验证输入后计算")


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


class AnswerCoverage(BaseModel):
    requirement_id: str = Field(min_length=1, max_length=20, description="必须使用问题清单中的编号，如Q1")
    aspect: str = Field(min_length=1, max_length=200,
                        description="该问题实际要求回答的一个要点；同一问题可有多个要点，不是思维过程")
    status: Literal["answered", "insufficient", "withheld"] = Field(
        description="已回答、缺证据、因整体拒答未输出；不能把指路或背景当成已回答")
    claim_indices: list[Annotated[int, Field(strict=True, ge=1)]] = Field(
        description="回答该要点的claims序号，从1开始；未回答时为空数组")
    missing_information: str | None = Field(
        description="未回答时明确缺口或整体拒答原因；已回答为null")


class GenerationAnswerDraft(BaseModel):
    """Required live-generation contract; old stored drafts/public APIs stay unchanged."""

    coverage: list[AnswerCoverage] = Field(
        min_length=1, max_length=32,
        description="先识别各问题的必答要点，再生成对应结论；所有问题编号都须出现")
    answerable: bool
    claims: list[AnswerClaim] = Field(default_factory=list)
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
