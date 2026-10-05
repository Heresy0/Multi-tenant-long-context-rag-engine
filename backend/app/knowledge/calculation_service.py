"""Small deterministic calculator with provenance validation; no eval/code/network.

Checks only supported operations. Verified outputs are rendered by the program,
never used to exempt arbitrary surrounding prose from numeric validation.
"""
import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal

from .answer_models import AnswerClaim


class CalculationError(ValueError):
    pass


@dataclass(frozen=True)
class CalculationResult:
    rendered_text: str
    result: str
    inputs: tuple[str, ...]
    model_result_matches: bool | None = None


def normalized(text):
    return "".join(unicodedata.normalize("NFKC", str(text)).split()).replace("|", "")


def decimal_value(value):
    if not re.fullmatch(r"-?\d+(?:\.\d{1,6})?", value):
        raise CalculationError("输入不是受支持的有限十进制数")
    result = Decimal(value)
    if abs(result) > Decimal("1000000000"):
        raise CalculationError("数值超出计算范围")
    return result


def clock(value):
    match = re.fullmatch(r"(\d{1,2}):(\d{2})(?::(\d{2}))?", value)
    if not match:
        raise CalculationError("起始时刻必须为HH:MM或HH:MM:SS")
    h, m, s = map(int, (match[1], match[2], match[3] or "0"))
    if h > 23 or m > 59 or s > 59:
        raise CalculationError("时刻无效")
    return h * 3600 + m * 60 + s


def compute(claim: AnswerClaim, context, question):
    request = claim.calculation
    if request is None:
        raise CalculationError("没有计算请求")
    sources = {item.citation_id: item.content for item in context.items}
    values, labels = [], []
    evidence_operands = []
    for operand in request.inputs:
        if operand.source == "问题":
            source_text = question
        elif operand.source in sources and operand.source in claim.citations:
            source_text = sources[operand.source]
        else:
            raise CalculationError("计算输入未绑定到本结论引用")
        quote = normalized(operand.quote)
        # A shortened quote must not turn 1100元 or -100元 into 100元,
        # nor truncate the seconds component of a source clock.
        prefix = r"(?<![\d.:\-])" if quote[:1].isdigit() or quote.startswith("-") else ""
        suffix = r"(?![\d.:])" if quote[-1:].isdigit() else ""
        if not re.search(prefix + re.escape(quote) + suffix, normalized(source_text)):
            raise CalculationError("计算输入摘录不在所声明来源中")
        if operand.unit == "元" and re.search(r"美元|港元|港币|日元|欧元|英镑|USD|HKD|JPY|EUR|GBP", source_text, re.I):
            raise CalculationError("当前金额计算仅支持明确的人民币元，不进行币种换算")
        if operand.unit == "时刻":
            value = clock(operand.value)
            tokens = re.findall(r"(?<![\d:])\d{1,2}:\d{2}(?::\d{2})?(?![\d:])", quote)
            if len(tokens) != 1 or clock(tokens[0]) != value:
                raise CalculationError("起始时刻与摘录不一致")
        else:
            value = decimal_value(operand.value)
            tokens = re.findall(r"(?<![\d.])(-?\d+(?:\.\d+)?)" + re.escape(operand.unit) + r"(?![a-zA-Z])", quote)
            if len(tokens) != 1 or decimal_value(tokens[0]) != value:
                raise CalculationError("数值或单位与摘录不一致")
        if operand.source != "问题":
            evidence_operands.append(operand)
        values.append(value)
        labels.append(f"{operand.value}{'' if operand.unit == '时刻' else operand.unit}")
    if not evidence_operands:
        raise CalculationError("计算必须有知识库证据输入")
    if request.operation == "time_add":
        if request.inputs[0].unit != "时刻" or any(op.unit not in ("秒", "分钟", "小时") for op in request.inputs[1:]):
            raise CalculationError("时间累加需要一个起始时刻和正时长")
        if any(value <= 0 for value in values[1:]):
            raise CalculationError("时长必须大于0")
        seconds = sum(value * {"秒": 1, "分钟": 60, "小时": 3600}[op.unit]
                      for op, value in zip(request.inputs[1:], values[1:]))
        if seconds != int(seconds) or seconds > 7 * 86400:
            raise CalculationError("仅支持整秒及最多7日的累计时长")
        # Retry schedules require the complete cited sequence, in source order.
        if re.search(r"重试|投递", question):
            if not (re.search(r"无\s*Retry-After|没有\s*Retry-After|不考虑\s*Retry-After", question, re.I)
                    and re.search(r"忽略.*(?:传输|请求|网络|处理).*耗时|每次.*立即失败|之后.*立即失败", question)):
                raise CalculationError("重试绝对时刻需要明确无Retry-After且忽略请求耗时的前提")
            attempt = re.search(r"第\s*(\d+)\s*次(?:投递|发送)", question)
            if not attempt or int(attempt[1]) - 1 != len(values) - 1:
                raise CalculationError("目标投递次数与重试间隔数量不一致")
            quoted_order = []
            for source in dict.fromkeys(op.source for op in request.inputs[1:]):
                if source == "问题":
                    raise CalculationError("重试间隔必须来自已引用的默认序列")
                text = normalized(sources[source])
                schedule = re.search(r"(?:默认)?间隔(?:依次)?(?:为|是)([^。;；\n]{1,220})", text)
                if not schedule:
                    raise CalculationError("引用中没有明确重试间隔序列")
                quoted_order.extend((decimal_value(v), u) for v, u in re.findall(
                    r"(\d+(?:\.\d+)?)(秒|分钟|小时)", schedule[1]))
            expected = [(decimal_value(op.value), op.unit) for op in request.inputs[1:]]
            if quoted_order[:len(expected)] != expected:
                raise CalculationError("重试间隔遗漏、重排或与默认序列不一致")
        total = int(values[0] + seconds)
        days, remaining = divmod(total, 86400)
        result = f"{remaining // 3600:02d}:{remaining % 3600 // 60:02d}:{remaining % 60:02d}"
        if days:
            result = f"第{days + 1}日{result}"
        matches = normalized(request.result) == result if request.result is not None else None
        text = f"按所引用的时长计算：{' + '.join(labels)} = {result}。"
    else:
        if (request.operation == "money_sum" and not re.search(r"合计|总额|总计|一共|求和|相加", question)
                or request.operation == "money_difference" and not re.search(r"差额|相差|差多少|差价|扣除|减去", question)):
            raise CalculationError("问题没有明确请求此金额运算")
        if any(op.unit != "元" for op in request.inputs):
            raise CalculationError("金额运算只接受元，不自动混合币种或单位")
        if request.operation == "money_difference" and len(values) != 2:
            raise CalculationError("金额差额只接受两个输入")
        value = sum(values) if request.operation == "money_sum" else values[0] - values[1]
        result = format(value.normalize(), "f")
        try:
            matches = decimal_value(request.result) == value if request.result is not None else None
        except CalculationError:
            matches = False
        sign = " + " if request.operation == "money_sum" else " - "
        text = f"按所引用金额计算：{sign.join(labels)} = {result}元。"
    return CalculationResult(text, result, tuple(labels), matches)
