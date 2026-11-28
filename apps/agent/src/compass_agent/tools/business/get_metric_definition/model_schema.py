"""Model-facing contract; execution retains the full internal domain model."""

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "asset_id": {
            "type": "string",
            "description": "已发布指标资产 ID。",
        },
    },
    "required": ["asset_id"],
}
