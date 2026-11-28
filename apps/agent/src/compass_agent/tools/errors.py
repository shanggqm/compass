"""Bounded, safe repair instructions shared by admission and execution."""

from dataclasses import dataclass
from typing import Literal

from compass_agent.tools.contracts import ToolRejected, json_bytes

TOOL_ERROR_MAX_BYTES = 4096
ErrorCategory = Literal[
    "fix_arguments", "switch_tool", "clarify_with_user", "retry_transient", "permission_denied"
]


@dataclass(frozen=True, slots=True)
class ActionableToolError:
    code: str
    category: ErrorCategory
    message: str
    retryable: bool = False
    field: str | None = None
    candidates: tuple[str, ...] = ()
    next_action: str | None = None
    suggested_tool: str | None = None

    def payload(self) -> dict:
        result = {
            "status": "error",
            "code": self.code[:100],
            "category": self.category,
            "message": self.message[:400],
            "retryable": self.retryable,
            "field": self.field[:160] if self.field else None,
            "candidates": [v[:80] for v in self.candidates[:8]],
            "nextAction": self.next_action[:240] if self.next_action else None,
            "suggestedTool": self.suggested_tool[:80] if self.suggested_tool else None,
        }
        while len(json_bytes(result)) > TOOL_ERROR_MAX_BYTES and result["candidates"]:
            result["candidates"].pop()
        return result


class ActionableToolRejected(ToolRejected):
    def __init__(self, error: ActionableToolError):
        self.error = error
        super().__init__(error.code)


def invalid_arguments(error) -> ActionableToolRejected:
    # Never echo received values, complete schemas or exception text into public receipts.
    path = ".".join(str(v) for v in error.absolute_path)
    candidates = ()
    if error.validator == "required":
        missing = [k for k in error.validator_value if k not in error.instance]
        path = ".".join(filter(None, [path, missing[0] if missing else ""]))
    elif error.validator == "enum":
        candidates = tuple(str(v) for v in error.validator_value)
    return ActionableToolRejected(
        ActionableToolError(
            "invalid_parameters",
            "fix_arguments",
            "参数不符合工具声明，请修正标明的字段。",
            field=path or None,
            candidates=candidates,
            next_action="按参数说明修正后重试。",
        )
    )
