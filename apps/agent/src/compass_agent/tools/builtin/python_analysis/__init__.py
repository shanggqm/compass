"""Authorized input references and code bounds for the Python sandbox tool."""

from dataclasses import replace

from compass_agent.prompt_assets import tool_text
from compass_agent.schemas import ToolDefinition, ToolFunction
from compass_agent.tools.builtin.python_analysis.evidence import calculation
from compass_agent.tools.builtin.python_analysis.model_schema import SCHEMA
from compass_agent.tools.catalog_contracts import ToolSpec
from compass_agent.tools.contracts import (
    RegisteredTool,
    SandboxBinding,
    ToolGovernance,
    ToolPolicy,
    ToolRejected,
    json_bytes,
)
from compass_agent.tools.registry import bounded_payload, parse_arguments


def fingerprint(args, context=None):
    # Files are mutable across Runs. Code/input equality cannot identify the effect
    # of an invocation that reads or writes the owner's shared workspace.
    return None


def python_analysis_tool() -> RegisteredTool:
    def authorize(args, context):
        if not isinstance(args, dict) or not isinstance(args.get("code"), str):
            raise ToolRejected("invalid_parameters")
        source = context.sources.get(args.get("source_query_id"))
        if source is None or source.owner != context.owner or source.run_id != context.run_id:
            raise ToolRejected("sandbox_source_denied")
        actual = len(json_bytes(source.data))
        if actual > 128 * 1024:
            from compass_agent.tools.errors import ActionableToolError, ActionableToolRejected

            raise ActionableToolRejected(
                ActionableToolError(
                    "sandbox_input_limit",
                    "fix_arguments",
                    f"查询结果体积（{actual} 字节）超过 Python 沙箱单次输入上限（131072 字节）。"
                    "请先通过 database_query 或 query_metric 按分析维度聚合、过滤或限制返回列后"
                    "再传入新的 source_query_id。",
                    field="source_query_id",
                    next_action="先聚合或过滤查询结果，再传入新的结果引用。",
                )
            )
        if len(args["code"].encode()) > 16 * 1024:
            raise ToolRejected("sandbox_input_limit")

    return RegisteredTool(
        "python_analysis",
        ToolDefinition(
            type="function",
            function=ToolFunction(
                name="python_analysis",
                description=(tool_text("python_analysis")),
                parameters={
                    "type": "object",
                    "properties": {
                        "source_query_id": {"type": "string", "maxLength": 128},
                        "code": {"type": "string", "minLength": 1, "maxLength": 16384},
                    },
                    "required": ["source_query_id", "code"],
                    "additionalProperties": False,
                },
            ),
        ),
        ToolPolicy("local_artifact", "none"),
        SandboxBinding(),
        parse_arguments,
        authorize,
        bounded_payload,
        parallel_safe=True,
        evidence_adapter=calculation,
        fingerprint=fingerprint,
    )


SPEC = ToolSpec(
    "python_analysis",
    "builtin",
    "python",
    lambda context: python_analysis_tool().definition,
    lambda context: context.sandbox,
    parallel_safe=True,
    factory=python_analysis_tool,
    fingerprint=fingerprint,
)


SPEC = replace(
    SPEC,
    governance=ToolGovernance(
        channel="scene_routed",
        pack="deep_analysis_pack",
        default_scenes=frozenset(
            [
                "SC-02",
                "SC-03",
                "SC-04",
                "SC-05",
                "SC-06",
                "SC-07",
                "SC-09",
                "SC-10",
                "SC-11",
                "SC-12",
                "SC-13",
                "SC-14",
                "SC-15",
                "SC-17",
                "SC-18",
                "SC-19",
                "SC-20",
                "SC-22",
                "SC-23",
                "SC-24",
                "SC-25",
                "SC-26",
                "SC-27",
                "SC-28",
                "SC-31",
                "SC-32",
                "SC-34",
                "SC-35",
                "SC-40",
            ]
        ),
    ),
)


def model_definition(context):
    return ToolDefinition(
        function=ToolFunction(
            name="python_analysis",
            description=tool_text("python_analysis"),
            parameters=SCHEMA,
        )
    )


SPEC = replace(SPEC, legacy_definition=SPEC.definition, definition=model_definition)
