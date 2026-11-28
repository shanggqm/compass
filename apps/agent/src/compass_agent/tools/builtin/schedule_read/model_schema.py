"""Model-facing contract; execution retains the full internal domain model."""

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "query": {
            "type": "string",
            "description": ("按任务名称或分析问题关键词筛选；省略则列出全部未删除任务。"),
        },
        "scheduleId": {
            "type": "string",
            "description": "按任务 ID 精确查看单个任务及执行历史。",
        },
    },
}
