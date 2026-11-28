"""Model-facing contract; execution retains the full internal domain model."""

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "sql": {
            "type": "string",
            "description": (
                "单条只读 PostgreSQL SELECT 语句，支持表结构查询及业务明细、聚合查询。"
                "业务表查询与 information_schema 元数据查询必须分开调用；"
                "禁止在同一 SQL 中通过 JOIN、CTE、子查询或 UNION / UNION ALL 混用两类表。"
            ),
        },
    },
    "required": ["sql"],
}
