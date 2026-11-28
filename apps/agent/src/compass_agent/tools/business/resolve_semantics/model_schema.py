"""Model-facing contract; execution retains the full internal domain model."""

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "mention": {
            "type": "string",
            "description": "用户问题中的原始指标名称（如 '净销售额'）。",
        },
        "metric_id": {
            "type": "string",
            "description": ("已确认的正式指标资产 ID；存在同名多口径歧义时省略以触发用户选择卡。"),
        },
        "start": {
            "type": "string",
            "description": "统计起始日期（YYYY-MM-DD，含当日）。",
        },
        "end": {
            "type": "string",
            "description": "统计结束日期（YYYY-MM-DD，含当日）。",
        },
        "group_by": {
            "type": "string",
            "description": ("分组拆解维度编码（如 region、channel）；总体汇总省略。"),
        },
        "filters": {
            "type": "object",
            "description": ('维度等值筛选键值对（如 {"channel": "online"}）。'),
        },
    },
    "required": ["mention"],
}
