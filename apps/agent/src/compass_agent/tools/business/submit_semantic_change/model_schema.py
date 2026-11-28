"""Model-facing contract; execution retains the full internal domain model."""

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "version_id": {
            "type": "string",
            "description": "已通过校验的语义草稿版本 ID。",
        },
        "expected_digest": {
            "type": "string",
            "description": ("review_semantic_change 校验通过回执返回的 digest 摘要串。"),
        },
        "note": {
            "type": "string",
            "description": ("提交专家评审的变更原因与业务影响简述（<=160 字）。"),
        },
    },
    "required": ["version_id", "expected_digest", "note"],
}
