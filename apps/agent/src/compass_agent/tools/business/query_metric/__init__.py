"""通过已发布执行绑定按期间、维度和过滤查询正式指标。比较时传两个可比期间。返回受信结果引用、口径、单位与数据截止。"""

from dataclasses import replace
from typing import Literal
from uuid import UUID

from pydantic import Field

from compass_agent.prompt_assets import tool_text
from compass_agent.schemas import ToolDefinition, ToolFunction
from compass_agent.semantic.tool import Arguments, Parameters, tool
from compass_agent.tools.business.query_metric.model_schema import SCHEMA
from compass_agent.tools.contracts import ToolGovernance


class Input(Arguments):
    query_ref: UUID
    operation: Literal["query", "compare", "drilldown"] = "query"
    parameters: Parameters | None = None
    comparison: Parameters | None = Field(default=None, description="比较时填写用户所指的对照期。")
    parent_group: dict[str, str | int | bool] | Literal["total"] | None = Field(
        default=None, description="下钻起点。填写 total 或已绑定的父级筛选条件。"
    )


BASE = tool("query_metric", "query", "query", Input)


def registration(catalog):
    from hashlib import sha256

    from compass_agent.llm.continuation import canonical

    assert BASE.context_factory and catalog.semantics
    service = catalog.semantics

    def fingerprint(args, context=None):
        plan = service.query_identity(args["query_ref"])
        value = {
            "tool": "query_metric",
            "plan": plan,
            "args": {key: value for key, value in args.items() if key != "query_ref"},
        }
        return sha256(canonical(value)).hexdigest(), "query"

    def normalize(raw, context):
        from datetime import date, timedelta

        args = dict(raw)
        comparison = args.pop("compare_to", None)
        if comparison is not None:
            params = service.query_identity(args["query_ref"])["parameters"]
            start, end = date.fromisoformat(params["start"]), date.fromisoformat(params["end"])
            if comparison == "previous":
                previous_start, previous_end = start - (end - start), start
            else:
                first, last = comparison.split("..", 1)
                previous_start = date.fromisoformat(first)
                previous_end = date.fromisoformat(last) + timedelta(days=1)
            args.update(
                operation="compare",
                comparison={
                    **params,
                    "start": previous_start.isoformat(),
                    "end": previous_end.isoformat(),
                },
            )
        if "drilldown_from" in args:
            if comparison is not None:
                from compass_agent.tools.contracts import ToolRejected

                raise ToolRejected("invalid_parameters")
            args.update(operation="drilldown", parent_group=args.pop("drilldown_from") or "total")
        return args

    return replace(
        BASE.context_factory(catalog), fingerprint=fingerprint, normalize_input=normalize
    )


SPEC = replace(BASE, context_factory=registration)


SPEC = replace(
    SPEC, governance=ToolGovernance(channel="always_on", pack="data_base", subagent_allowed=True)
)


def model_definition(context):
    return ToolDefinition(
        function=ToolFunction(
            name="query_metric",
            description=tool_text("query_metric"),
            parameters=SCHEMA,
        )
    )


SPEC = replace(SPEC, legacy_definition=SPEC.definition, definition=model_definition)
