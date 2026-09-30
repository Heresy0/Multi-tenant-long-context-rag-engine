import json


def event_message(
    event: str,
    **fields: object,
) -> str:
    """生成便于日志平台解析的紧凑 JSON 事件。"""
    return json.dumps(
        {
            "event": event,
            **fields,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )
