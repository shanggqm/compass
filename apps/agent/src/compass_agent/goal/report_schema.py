"""Model-facing contract; execution retains the full internal domain model."""

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "summary": {
            "type": "string",
            "description": ("面向主 Agent 的结构化调查结论与关键数值摘要（Markdown）。"),
        },
        "evidenceIds": {
            "type": "array",
            "items": {
                "type": "string",
            },
            "description": ("支撑上述结论的本子任务工具回执 evidenceId 列表。"),
        },
        "gaps": {
            "type": "array",
            "items": {
                "type": "string",
            },
            "description": "受权限或数据限制未能核实的缺口说明；全部完成时省略。",
        },
        "status": {
            "type": "string",
            "enum": ["completed", "partial", "blocked"],
            "description": (
                "子任务完成态：全部完成传 completed，存在缺"
                "口传 partial，权限或数据受阻传 blocke"
                "d；省略时按 gaps 自动推导。"
            ),
        },
    },
    "required": ["summary"],
}
