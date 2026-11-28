"""Model-facing contract; execution retains the full internal domain model."""

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "query_ref": {
            "type": "string",
            "description": ("预注入上下文或 resolve_semantics 签发的查询引用 ID。"),
            "format": "uuid",
        },
        "compare_to": {
            "type": "string",
            "description": (
                "期间对比配置：传 'previous' 自动对比紧邻"
                "等长上期，或传 'YYYY-MM-DD..YYYY-"
                "MM-DD' 指定对照区间；普通查询省略。"
            ),
        },
        "drilldown_from": {
            "type": "object",
            "description": ("维度下钻的父级筛选键值（从总体汇总下钻传空对象 {}）；不下钻时省略。"),
        },
    },
    "required": ["query_ref"],
}
