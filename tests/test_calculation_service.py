import pytest
from pydantic import ValidationError

from backend.app.knowledge.answer_models import AnswerClaim, AnswerDraft, CalculationOperand, CalculationRequest
from backend.app.knowledge.answer_validation import AnswerValidator
from backend.app.knowledge.calculation_service import CalculationError, compute
from backend.app.knowledge.context_builder import ContextBuilder
from langchain_core.documents import Document

QUESTION = "Webhook首次于18:00:00立即失败，之后也立即失败，无Retry-After、忽略传输耗时，按默认间隔第6次投递何时发生？"
SCHEDULE = "最多追加5次自动重试。默认间隔依次为30秒、2分钟、10分钟、30分钟和2小时，按上一次失败后等待计算。"


def context(text=SCHEDULE):
    return ContextBuilder().build([Document(page_content=text, metadata={"source": "规范", "document_name": "规范"})])


def retry_claim(result="20:42:30"):
    inputs = [CalculationOperand(source="问题", quote="18:00:00", value="18:00:00", unit="时刻")]
    inputs.extend(CalculationOperand(source="资料1", quote=v + u, value=v, unit=u)
                  for v, u in [("30", "秒"), ("2", "分钟"), ("10", "分钟"), ("30", "分钟"), ("2", "小时")])
    return AnswerClaim(text="模型计算文本", citations=["资料1"],
                       calculation=CalculationRequest(operation="time_add", inputs=inputs, result=result))


def test_correct_schedule_is_verified_and_program_rendered():
    claim = retry_claim()
    result = compute(claim, context(), QUESTION)
    assert result.result == "20:42:30"
    claim.text = result.rendered_text
    assert AnswerValidator().validate(draft=AnswerDraft(answerable=True, claims=[claim]), context=context(), question=QUESTION).valid


@pytest.mark.parametrize("change", ["missing_interval", "reordered", "wrong_unit", "invented_quote", "uncited_source", "false_origin", "expression", "extra_prose"])
def test_forged_or_wrong_computations_are_rejected(change):
    claim = retry_claim()
    if change == "missing_interval":
        claim.calculation.inputs.pop(2)
    elif change == "reordered":
        claim.calculation.inputs[1], claim.calculation.inputs[2] = claim.calculation.inputs[2], claim.calculation.inputs[1]
    elif change == "wrong_unit":
        claim.calculation.inputs[1].unit = "小时"
    elif change == "invented_quote":
        claim.calculation.inputs[1].quote = "60秒"
    elif change == "uncited_source":
        claim.citations = ["资料99"]
    elif change == "false_origin":
        claim.calculation.inputs[1].source = "问题"
    elif change == "expression":
        claim.calculation.inputs[1].value = "__import__('os')"
    else:
        claim.text = compute(claim, context(), QUESTION).rendered_text + "实际营收123456元。"
        validation = AnswerValidator().validate(draft=AnswerDraft(answerable=True, claims=[claim]), context=context(), question=QUESTION)
        assert not validation.valid
        return
    with pytest.raises(CalculationError):
        compute(claim, context(), QUESTION)


def test_direct_claim_still_requires_literal_numbers():
    claim = AnswerClaim(text="结果是20:42:30", citations=["资料1"])
    assert not AnswerValidator().validate(draft=AnswerDraft(answerable=True, claims=[claim]), context=context(), question=QUESTION).valid


@pytest.mark.parametrize("operation,result", [("money_sum", "130.30"), ("money_difference", "70.1")])
def test_decimal_money_arithmetic(operation, result):
    ctx = context("项目甲100.20元；项目乙30.10元。")
    claim = AnswerClaim(text="金额", citations=["资料1"], calculation=CalculationRequest(
        operation=operation, result=result, inputs=[
            CalculationOperand(source="资料1", quote="100.20元", value="100.20", unit="元"),
            CalculationOperand(source="资料1", quote="30.10元", value="30.10", unit="元")]))
    computed = compute(claim, ctx, "金额合计是多少" if operation == "money_sum" else "金额相差多少")
    assert computed.result in ("130.3", "70.1")


def test_cross_midnight_requires_day_qualifier_and_no_code_operation():
    claim = retry_claim("第2日02:12:30")
    claim.calculation.inputs[0].value = "23:30:00"
    claim.calculation.inputs[0].quote = "23:30:00"
    assert compute(claim, context(), QUESTION.replace("18:00:00", "23:30:00")).result == "第2日02:12:30"
    claim.calculation.result = "02:12:30"
    computed = compute(claim, context(), QUESTION.replace("18:00:00", "23:30:00"))
    assert computed.result == "第2日02:12:30" and computed.model_result_matches is False
    with pytest.raises(ValidationError):
        CalculationRequest(operation="python_eval", inputs=claim.calculation.inputs, result="0")


@pytest.mark.parametrize('guess', ['21:42:30', None, '随便猜测'])
def test_valid_inputs_not_model_guess_authorize_calculation(guess):
    computed = compute(retry_claim(guess), context(), QUESTION)
    assert computed.result == '20:42:30'
    assert computed.model_result_matches is (None if guess is None else False)


@pytest.mark.parametrize("question", [
    QUESTION.replace("无Retry-After、", ""),
    QUESTION.replace("忽略传输耗时，", "").replace("立即失败", "失败").replace("之后也立即失败", "之后也失败"),
    QUESTION.replace("第6次投递", "第6次重试"),
])
def test_retry_requires_unambiguous_attempt_and_delay_premises(question):
    with pytest.raises(CalculationError):
        compute(retry_claim(), context(), question)


@pytest.mark.parametrize("source,quote,value", [
    ("金额1100元", "100元", "100"),
    ("金额-100元", "100元", "100"),
    ("金额100元港币", "100元", "100"),
    ("费用100美元", "100美元", "100"),
])
def test_money_rejects_truncated_quantities_and_foreign_currencies(source, quote, value):
    claim = AnswerClaim(text="计算", citations=["资料1"], calculation=CalculationRequest(
        operation="money_sum", result="110", inputs=[
            CalculationOperand(source="资料1", quote=quote, value=value, unit="元"),
            CalculationOperand(source="问题", quote="10元", value="10", unit="元")]))
    with pytest.raises(CalculationError):
        compute(claim, context(source), "和10元合计是多少")


def test_money_does_not_infer_requested_operation():
    claim = AnswerClaim(text="计算", citations=["资料1"], calculation=CalculationRequest(
        operation="money_sum", result="130.3", inputs=[
            CalculationOperand(source="资料1", quote="100.20元", value="100.20", unit="元"),
            CalculationOperand(source="资料1", quote="30.10元", value="30.10", unit="元")]))
    with pytest.raises(CalculationError, match="明确请求"):
        compute(claim, context("甲100.20元；乙30.10元"), "介绍这两个项目")


def test_shortened_clock_quote_cannot_hide_source_seconds():
    claim = retry_claim()
    claim.calculation.inputs[0].quote = "18:00"
    claim.calculation.inputs[0].value = "18:00"
    with pytest.raises(CalculationError):
        compute(claim, context(), QUESTION.replace("18:00:00", "18:00:59"))
