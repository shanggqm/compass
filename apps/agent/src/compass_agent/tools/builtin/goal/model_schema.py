"""Model-facing contract; execution retains the full internal domain model."""

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "action": {
            "type": "string",
            "enum": ["propose", "record", "complete", "block"],
            "description": (
                "动作：propose 立项待用户确认；record "
                "记阶段结论或受阻尝试；complete 全部达标后申"
                "请核验；block 缺外部决策或权限时挂起。"
            ),
        },
        "objective": {
            "type": "string",
            "description": ("action=propose 时的长任务总目标；action=block 时填阻塞原因。"),
        },
        "acceptance": {
            "type": "array",
            "items": {
                "type": "string",
            },
            "description": ("action=propose 时的可核验完成标准列表（1..5 项）。"),
        },
        "finding": {
            "type": "string",
            "description": ("action=record 时写入跨轮账本的阶段结论或失败原因摘要。"),
        },
        "evidenceIds": {
            "type": "array",
            "items": {
                "type": "string",
            },
            "description": ("action=record 支撑该结论的本轮工具 evidenceId 列表。"),
        },
        "acceptanceIndexes": {
            "type": "array",
            "items": {
                "type": "integer",
            },
            "description": (
                "action=record 时本结论已满足的验收项序"
                "号（1 起始，对应立项列表顺序）；未满足任何验收项时"
                "省略。"
            ),
        },
    },
    "required": ["action"],
}
