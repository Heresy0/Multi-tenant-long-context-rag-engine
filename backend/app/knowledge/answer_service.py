import time

from langchain_core.messages import (
    HumanMessage,
    SystemMessage,
)
from langchain_openai import ChatOpenAI
from sqlalchemy.orm import Session

from .answer_models import (
    AnswerDraft,
    AnswerResult,
    AnswerTimings,
    Citation,
)
from .answer_validation import AnswerValidator
from .calculation_service import CalculationError, compute
from ..config import Settings
from .context_builder import (
    BuiltContext,
    ContextBuilder,
)
from .prompts import ANSWER_SYSTEM_PROMPT
from .answer_constraints import complete_cited_table_conditions, generation_constraints
from .temporal import future_certainty_gap
from .retrieval_service import RetrievalService
from ..security.retrieval_scope import RetrievalScope
from ..evaluation.trace import record_context, record_documents, record_scope, record_calculations


REFUSAL_TEXT = (
    "根据当前知识库资料，暂时无法回答这个问题。"
)


def _elapsed_ms(started: float) -> float:
    return round(
        (time.perf_counter() - started) * 1000,
        2,
    )


class AnswerService:
    def __init__(
        self,
        *,
        settings: Settings,
        retrieval_service: RetrievalService,
        context_builder: ContextBuilder | None = None,
        validator: AnswerValidator | None = None,
    ) -> None:
        self._retrieval_service = retrieval_service
        self._context_builder = (
            context_builder or ContextBuilder()
        )
        self._validator = (
            validator or AnswerValidator()
        )

        llm = ChatOpenAI(
            model=settings.chat_model,
            base_url=settings.chat_base_url,
            api_key=settings.chat_api_key,
            temperature=0,
            reasoning_effort="none",
            timeout=60,
            max_retries=0,
        )

        self._structured_llm = (
            llm.with_structured_output(AnswerDraft)
        )

    def answer(
        self,
        question: str,
        *,
        scope: RetrievalScope,
        session: Session,
    ) -> AnswerResult:
        question = question.strip()

        if not question:
            raise ValueError("问题不能为空")
        record_scope(question, scope)

        total_started = time.perf_counter()
        timings: dict[str, float | None] = {
            "hybrid_retrieval_ms": None,
            "rerank_ms": None,
            "search_total_ms": 0.0,
            "context_build_ms": 0.0,
            "generation_ms": None,
            "validation_ms": None,
            "render_ms": None,
        }

        search_started = time.perf_counter()
        documents = self._retrieval_service.search(
            question,
            scope=scope,
            session=session,
        )
        # 防御性校验：缺少来源范围或越权的分块不得进入上下文/生成模型。
        documents = [document for document in documents if scope.contains(
            document.metadata.get("tenant_id"), document.metadata.get("knowledge_base_id"),
        )]
        record_documents("authorized_final", documents)
        timings["search_total_ms"] = _elapsed_ms(
            search_started
        )

        if documents:
            metadata = documents[0].metadata
            timings["hybrid_retrieval_ms"] = (
                self._metadata_latency(
                    metadata,
                    "retrieval_latency_ms",
                )
            )
            timings["rerank_ms"] = self._metadata_latency(
                metadata,
                "rerank_latency_ms",
            )

        context_started = time.perf_counter()
        context = self._context_builder.build(
            documents, question=question,
        )
        record_context(context)
        timings["context_build_ms"] = _elapsed_ms(
            context_started
        )

        future_gap = bool(context.items) and future_certainty_gap(question, context.items)
        if not context.items or future_gap:
            render_started = time.perf_counter()
            result = self._refusal(
                "缺少已确定覆盖该未来期间的正式制度，不能把现行规则说成未来承诺。"
                if future_gap else "没有检索到相关知识库资料。"
            )
            timings["render_ms"] = _elapsed_ms(
                render_started
            )
            return self._finish_result(
                result=result,
                timings=timings,
                total_started=total_started,
            )

        generation_started = time.perf_counter()
        draft = self._generate_draft(
            question=question,
            context=context,
        )
        timings["generation_ms"] = _elapsed_ms(
            generation_started
        )

        validation_started = time.perf_counter()
        complete_cited_table_conditions(question, draft, context)
        calculation_records = []
        for claim in draft.claims:
            if claim.calculation is None:
                continue
            try:
                calculation = compute(claim, context, question)
                claim.text = calculation.rendered_text
                calculation_records.append(dict(operation=claim.calculation.operation, status="verified",
                                                result=calculation.result, inputs=list(calculation.inputs)))
            except CalculationError as exc:
                calculation_records.append(dict(operation=claim.calculation.operation, status="rejected", reason=str(exc),
                                                proposed=claim.calculation.model_dump()))
        record_calculations(calculation_records)
        validation = self._validator.validate(
            draft=draft,
            context=context,
            question=question,
        )
        timings["validation_ms"] = _elapsed_ms(
            validation_started
        )

        if not validation.valid:
            render_started = time.perf_counter()
            result = self._refusal(
                "生成结果未通过证据与引用校验："
                + "；".join(validation.errors)
            )
            timings["render_ms"] = _elapsed_ms(
                render_started
            )
            return self._finish_result(
                result=result,
                timings=timings,
                total_started=total_started,
            )

        if not draft.answerable:
            render_started = time.perf_counter()
            result = self._refusal(
                draft.refusal_reason
                or "知识库资料不足。"
            )
            timings["render_ms"] = _elapsed_ms(
                render_started
            )
            return self._finish_result(
                result=result,
                timings=timings,
                total_started=total_started,
            )

        render_started = time.perf_counter()
        result = self._render_result(
            draft=draft,
            context=context,
        )
        timings["render_ms"] = _elapsed_ms(
            render_started
        )

        return self._finish_result(
            result=result,
            timings=timings,
            total_started=total_started,
        )

    @staticmethod
    def _metadata_latency(
        metadata: dict,
        key: str,
    ) -> float | None:
        value = metadata.get(key)

        if (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and value >= 0
        ):
            return round(float(value), 2)

        return None

    @staticmethod
    def _finish_result(
        *,
        result: AnswerResult,
        timings: dict[str, float | None],
        total_started: float,
    ) -> AnswerResult:
        completed_timings = dict(timings)
        completed_timings["total_ms"] = _elapsed_ms(
            total_started
        )

        return result.model_copy(
            update={
                "timings": AnswerTimings(
                    **completed_timings
                ),
            }
        )

    def _generate_draft(
        self,
        *,
        question: str,
        context: BuiltContext,
    ) -> AnswerDraft:
        result = self._structured_llm.invoke([
            SystemMessage(
                content=ANSWER_SYSTEM_PROMPT
            ),
            HumanMessage(
                content=(
                    f"用户问题：\n{question}\n\n"
                    f"作答检查：\n{generation_constraints(question)}\n\n"
                    f"知识库资料：\n{context.text}"
                )
            ),
        ])

        if not isinstance(result, AnswerDraft):
            result = AnswerDraft.model_validate(result)

        return result

    def _render_result(
        self,
        *,
        draft: AnswerDraft,
        context: BuiltContext,
    ) -> AnswerResult:
        item_map = {
            item.citation_id: item
            for item in context.items
        }

        answer_parts: list[str] = []
        used_citations: list[str] = []

        for claim in draft.claims:
            citation_text = "".join(
                f"[{citation_id}]"
                for citation_id in claim.citations
            )

            answer_parts.append(
                f"{claim.text.rstrip('。')}。"
                f"{citation_text}"
            )

            for citation_id in claim.citations:
                if citation_id not in used_citations:
                    used_citations.append(citation_id)

        citations = []

        for citation_id in used_citations:
            item = item_map[citation_id]
            citations.append(
                Citation(
                    citation_id=item.citation_id,
                    document_id=item.document_id,
                    document_name=item.document_name,
                    section_path=item.section_path,
                    chunk_id=item.chunk_id,
                    knowledge_base_id=item.knowledge_base_id,
                    knowledge_base_name=item.knowledge_base_name,
                )
            )

        return AnswerResult(
            answer="\n".join(answer_parts),
            answerable=True,
            citations=citations,
        )

    @staticmethod
    def _refusal(reason: str) -> AnswerResult:
        return AnswerResult(
            answer=REFUSAL_TEXT,
            answerable=False,
            citations=[],
            refusal_reason=reason,
        )
