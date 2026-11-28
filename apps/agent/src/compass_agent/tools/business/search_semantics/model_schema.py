"""Model-facing contract; execution retains the full internal domain model."""

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "query": {
            "type": "string",
            "description": "名称、别名或业务域关键词。",
        },
        "kind": {
            "type": "string",
            "enum": ["metric", "dimension", "entity", "term"],
            "description": "资产类别；省略查全部。",
        },
        "cursor": {
            "type": "string",
            "description": "翻页传上一页 next_cursor；首页省略。",
        },
    },
    "required": ["query"],
}
