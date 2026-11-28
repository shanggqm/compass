"""Model-facing contract; execution retains the full internal domain model."""

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "version_id": {
            "type": "string",
            "description": "草稿版本 ID。",
        },
    },
    "required": ["version_id"],
}
