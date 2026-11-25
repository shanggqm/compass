"""通过已发布执行绑定按期间、维度和过滤查询正式指标。比较时传两个可比期间。返回受信结果引用、口径、单位与数据截止。"""

from dataclasses import replace
from typing import Literal
from uuid import UUID

from pydantic import Field

from compass_agent.semantic.tool import Arguments, Parameters, tool


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

    return replace(BASE.context_factory(catalog), fingerprint=fingerprint)


SPEC = replace(BASE, context_factory=registration)
