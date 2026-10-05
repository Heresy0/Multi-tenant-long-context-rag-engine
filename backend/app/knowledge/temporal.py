"""Conservative business-time parsing; an as-of date is not a business date."""
import calendar
import re
from datetime import date, datetime, timedelta, timezone

from ..documents.policy_metadata import DATE, parse_date

_REFERENCE = re.compile(r"(?:截至|查询日期(?:为|是|[:：])?)\s*" + DATE)
_MONTH = re.compile(r"(?<!\d)(\d{4})年(\d{1,2})月")
_YEAR = re.compile(r"(?<!\d)(\d{4})年")


def business_today():
    return datetime.now(timezone(timedelta(hours=8))).date()


def _periods(text):
    spans, periods = [], set()
    for match in re.finditer(DATE, text):
        spans.append(match.span())
        value = parse_date(match.group(1))
        if value:
            periods.add((value, value))
    for pattern, kind in ((_MONTH, 'month'), (_YEAR, 'year')):
        for match in pattern.finditer(text):
            if any(match.start() < end and match.end() > start for start, end in spans):
                continue
            spans.append(match.span())
            try:
                year = int(match.group(1))
                if kind == 'month':
                    month = int(match.group(2))
                    periods.add((date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])))
                else:
                    periods.add((date(year, 1, 1), date(year, 12, 31)))
            except ValueError:
                continue
    return periods


def query_period(question, today=None):
    # Remove reference dates only when another explicit business time exists.
    business = _REFERENCE.sub('', question)
    periods = _periods(business)
    if len(periods) == 1:
        return next(iter(periods))
    if len(periods) > 1:
        return None  # Preserve evidence for comparisons/ambiguous ranges.
    periods = _periods(question)
    if len(periods) == 1:
        return next(iter(periods))
    if not periods and re.search(r'当前|现在|现行|最新', question):
        value = today or business_today()
        return value, value
    return None


def clean_followup_time(current_question, rewritten):
    """Remove only an inherited reference prefix, never user-supplied conditions."""
    if _REFERENCE.search(current_question) or not _periods(current_question):
        return rewritten
    if _periods(current_question).issubset(_periods(_REFERENCE.sub('', rewritten))):
        return _REFERENCE.sub('', rewritten).lstrip('，,；; ：:')
    return rewritten  # Do not guess when the model dropped/changed business time.


def future_certainty_gap(question, items, today=None):
    """Do not turn an open-ended current policy into a promised future rule."""
    period = query_period(question, today=today)
    reference = today or business_today()
    if not period or period[0] <= reference or not re.search(r'已(?:经)?确定|承诺|保证|确定的', question):
        return False
    for item in items:
        policy = item.policy or {}
        start, end = parse_date(policy.get('effective_from')), parse_date(policy.get('effective_to'))
        if policy.get('business_status') in ('草案', '已废止', '废止'):
            continue
        # A review date is deliberately not an end date. Require an explicit
        # covering period or an already published future-effective policy.
        if start and start <= period[0] and (end and end >= period[1] or start > reference and end is None):
            return False
    return True
