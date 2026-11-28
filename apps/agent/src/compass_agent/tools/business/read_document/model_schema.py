"""Model-facing contract; execution retains the full internal domain model."""

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "ref": {
            "type": "string",
            "description": ("本轮 search_documents 返回的段落引用 ID。"),
        },
        "offset": {
            "type": "integer",
            "enum": [-1, 0, 1],
            "description": ("段落偏移：0 当前段（默认），-1 上一段，1 下一段。"),
        },
    },
    "required": ["ref"],
}
