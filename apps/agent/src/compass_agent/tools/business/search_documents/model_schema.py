"""Model-facing contract; execution retains the full internal domain model."""

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "query": {
            "type": "string",
            "description": ("检索关键词（支持政策名、活动代号、实体名或业务术语）。"),
        },
        "kind": {
            "type": "string",
            "enum": ["knowledge", "history", "external"],
            "description": (
                "资料类型：制度规范(knowledge)、历史复盘("
                "history)、外部资料(external)；不确"
                "定时省略。"
            ),
        },
        "businessDate": {
            "type": "string",
            "description": (
                "排查历史活动或旧版制度时填业务发生日（YYYY-MM-DD）；查现行有效版本时省略。"
            ),
        },
    },
    "required": ["query"],
}
