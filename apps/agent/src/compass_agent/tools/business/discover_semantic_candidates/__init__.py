"""从授权素材或同会话临时查询回执发现候选。说明来源、位置、置信度、公式与缺项。保存前向用户展示可编辑预览并等待确认。"""

from dataclasses import replace
from typing import Literal
from uuid import UUID

from pydantic import Field

from compass_agent.prompt_assets import tool_text
from compass_agent.schemas import ToolDefinition, ToolFunction
from compass_agent.semantic.draft_fields import SuggestedDraftFields
from compass_agent.semantic.tool import Arguments, tool
from compass_agent.tools.business.discover_semantic_candidates.model_schema import SCHEMA
from compass_agent.tools.contracts import ToolGovernance


class Input(Arguments):
    revise_metric: str | None = Field(
        default=None,
        min_length=1,
        max_length=160,
        description="修订已保存的指标时填现有名称、唯一代码或资产 ID，fields 只填本次修改。",
    )
    source_refs: list[UUID] | None = Field(default=None, max_length=8)
    owner_scope_id: UUID | None = None
    usage_scope_id: UUID | None = None
    result_ref: UUID | None = Field(
        default=None,
        description="可选。用户指的是刚才的查询时可以省略，系统会在当前会话查找并让用户选择。",
    )
    trigger: Literal["requested", "suggest"] = Field(
        default="requested",
        description="用户要求保存或选择时为 requested。只有主动推荐时填 suggest。",
    )
    reuse_reason: str | None = Field(
        default=None,
        min_length=1,
        max_length=400,
        description="主动推荐时说明哪些经营决策会跨期重复使用这项计算。",
    )
    fields: SuggestedDraftFields | None = None


SPEC = tool("discover_semantic_candidates", "discover", "draft", Input)


SPEC = replace(
    SPEC,
    governance=ToolGovernance(
        channel="user_selected", pack="semantic_govern", requires_semantic_maintainer=True
    ),
)


def normalize_input(raw, _context):
    args = dict(raw)
    if any(k in args for k in ("mode", "spec", "resultRef", "targetMetric")):
        mode = args.pop("mode", "save_draft")
        args["trigger"] = "suggest" if mode == "suggest" else "requested"
        if "spec" in args:
            args["fields"] = args.pop("spec")
        if "resultRef" in args:
            args["result_ref"] = args.pop("resultRef")
        if "targetMetric" in args:
            args["revise_metric"] = args.pop("targetMetric")
        if mode == "suggest":
            args["reuse_reason"] = args.get("fields", {}).get("summary", "复用本次查询口径")
        if mode == "revise" and not args.get("revise_metric"):
            from compass_agent.tools.contracts import ToolRejected

            raise ToolRejected("invalid_parameters")
    return args


SPEC = replace(SPEC, normalize_input=normalize_input)


def model_definition(context):
    return ToolDefinition(
        function=ToolFunction(
            name="discover_semantic_candidates",
            description=tool_text("discover_semantic_candidates"),
            parameters=SCHEMA,
        )
    )


SPEC = replace(SPEC, legacy_definition=SPEC.definition, definition=model_definition)
