"""Model-facing contract; execution retains the full internal domain model."""

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "completion": {
            "type": "string",
            "enum": ["complete", "partial"],
            "description": (
                "逐项核对用户要求与实际结果；全部完成选 complete，"
                "仍有未解决事项选 partial 并在回答中说明。"
            ),
        },
        "mode": {
            "type": "string",
            "enum": ["plain", "evidence"],
            "description": (
                "回答模式：plain 用于概念解释、操作说明或追问澄"
                "清；evidence 用于基于本轮检索/查询结果生成"
                "带 ^[1]^ 证据角标的循证回答。"
            ),
        },
        "artifactFormat": {
            "type": "string",
            "enum": ["markdown", "csv", "dashboard"],
            "description": (
                "仅当用户明确要求导出可下载文件时填写（markdow"
                "n 报告 / csv 明细表 / dashboard"
                " 看板）；常规问答省略。"
            ),
        },
    },
    "required": ["mode"],
}
