"""Conservative business-policy metadata, distinct from index state/version.

Only labelled source fields are extracted. Missing values stay unknown. Explicit
administrator metadata can be persisted under ``policy`` in metadata_json.
"""
import re
import unicodedata
from datetime import date

POLICY_METADATA_VERSION = "policy-v1"
DATE = r"(\d{4}(?:年\d{1,2}月\d{1,2}日|[-/]\d{1,2}[-/]\d{1,2}))"


def parse_date(value):
    try:
        parts = re.sub(r"[年/月]", "-", str(value)).replace("日", "").split("-")
        return date(*(int(part) for part in parts)) if len(parts) == 3 else None
    except ValueError:
        return None


def extract_policy(text, title=""):
    text = unicodedata.normalize("NFKC", text)
    # Separators include table cells; never treat review date as expiry.
    def field(label, value):
        match = re.search(label + r"\s*[:|]?\s*" + value, text)
        return match.group(1).strip() if match else None

    policy = {"schema_version": POLICY_METADATA_VERSION, "origin": "labelled_source_fields"}
    values = {
        "document_code": field("文档编号", r"([A-Z][A-Z0-9-]+)"),
        "business_version": field("版本", r"(\d+(?:\.\d+)+)"),
        "effective_from": field("生效日期", DATE),
        "effective_to": field("(?:失效日期|有效期截至|适用期结束)", DATE),
        "applicability": field("适用范围", r"([^\n|;。]{1,160})"),
        "business_status": field("状态", r"(历史归档|已归档|归档|现行|已废止|废止|草案)"),
    }
    period = re.search(r"(?:适用期间|适用日期|有效期间|有效期)\s*(?:为|[:|])?\s*" + DATE + r"\s*(?:至|到|—|~|-)\s*" + DATE, text)
    if period:
        values.update(effective_from=period.group(1), effective_to=period.group(2))
    for key, value in values.items():
        if value:
            if key.startswith("effective_"):
                parsed = parse_date(value)
                if not parsed:
                    continue
                value = parsed.isoformat()
            policy[key] = value
    if (policy.get("effective_from") and policy.get("effective_to")
            and policy["effective_from"] > policy["effective_to"]):
        policy.pop("effective_from")
        policy.pop("effective_to")
        policy["metadata_warning"] = "有效期冲突，需管理员核实"
    # Topics refer to the document title, not incidental mentions in its body.
    policy["topics"] = [label for label, pattern in (
        ("webhook", r"webhook"), ("api", r"API|开放平台"), ("travel", r"差旅|住宿")
    ) if re.search(pattern, title, re.I)]
    return policy


def enrich_chunks(chunks, title):
    policy = extract_policy("\n".join(chunk.page_content for chunk in chunks), title)
    for chunk in chunks:
        chunk.metadata["policy"] = dict(policy)
    return chunks


def policy_label(policy):
    if not isinstance(policy, dict):
        return ""
    labels = {"document_code": "文档编号", "business_version": "业务版本", "effective_from": "生效日期",
              "effective_to": "失效日期", "business_status": "业务状态", "applicability": "适用范围",
              "metadata_warning": "元数据警告"}
    return "；".join(f"{label}：{policy[key]}" for key, label in labels.items() if policy.get(key))
