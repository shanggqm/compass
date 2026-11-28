"""Model-facing contract; execution retains the full internal domain model."""

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "todos": {
            "type": "array",
            "description": "完整步骤列表（2..6 项），每次全量覆盖最新状态。",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["step", "status"],
                "properties": {
                    "step": {
                        "type": "string",
                        "description": "本步具体子问题或动作。",
                    },
                    "status": {
                        "type": "string",
                        "enum": ["pending", "in_progress", "done", "skipped"],
                        "description": "步骤状态（保持 1 项 in_progress）。",
                    },
                },
            },
        },
    },
    "required": ["todos"],
}
