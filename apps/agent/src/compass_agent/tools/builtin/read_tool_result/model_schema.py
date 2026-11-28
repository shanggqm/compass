"""Model-facing contract; execution retains the full internal domain model."""

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "ref": {
            "type": "string",
            "description": "上游回执返回的 resultId 或 ref。",
        },
        "cursor": {
            "type": "string",
            "description": "续页传上一页 nextCursor；首次省略。",
        },
        "offset": {
            "type": "integer",
            "description": ("首次读取 0 起始行号或字符偏移；传 cursor 时忽略。"),
        },
        "limit": {
            "type": "integer",
            "description": "单次条数上限（1..200，默认 50）。",
        },
    },
    "required": ["ref"],
}
