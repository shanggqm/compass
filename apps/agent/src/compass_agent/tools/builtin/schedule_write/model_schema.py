"""Model-facing contract; execution retains the full internal domain model."""

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "scheduleId": {
            "type": "string",
            "description": (
                "修改、启停或删除已有任务时必填目标 ID（先经 schedule_read 确认）；新建任务时省略。"
            ),
        },
        "name": {
            "type": "string",
            "description": "定时任务名称（新建时必填，修改时可选）。",
        },
        "question": {
            "type": "string",
            "description": ("每次定时触发执行的分析问题（新建时必填，修改时可选）。"),
        },
        "schedule": {
            "type": "string",
            "description": (
                "触发周期：支持简写 'daily 09:00'、'w"
                "eekly MON 09:00'（MON..SUN）"
                "或五段 Cron（新建时必填）。"
            ),
        },
        "status": {
            "type": "string",
            "enum": ["enabled", "paused", "deleted"],
            "description": ("任务状态：默认 enabled；暂停传 paused；软删除传 deleted。"),
        },
    },
}
