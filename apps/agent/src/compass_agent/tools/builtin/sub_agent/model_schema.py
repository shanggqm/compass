"""Model-facing contract; execution retains the full internal domain model."""

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "tasks": {
            "type": "array",
            "description": ("并行执行的独立只读子任务列表（1..4 项，name 互不重复）。"),
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "objective"],
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "子任务短标题（用于界面卡片展示，<=16 字）。",
                    },
                    "objective": {
                        "type": "string",
                        "description": (
                            "独立可执行的调查问题、统计期间、口径约束与期望交回的指标结论。"
                        ),
                    },
                    "inputRefs": {
                        "type": "array",
                        "items": {
                            "type": "string",
                        },
                        "description": (
                            "按引用传入父任务已取得的 evidenceId、resultId 或账本条目 ID。"
                        ),
                    },
                    "maxSteps": {
                        "type": "integer",
                        "minimum": 3,
                        "maximum": 8,
                        "description": (
                            "子任务模型步数，默认 3、最多 8，受父任务剩余总预算约束。"
                            "按探查、取数、分析与最后一次 report_to_parent 汇报所需步骤分配。"
                        ),
                    },
                },
            },
        },
    },
    "required": ["tasks"],
}
