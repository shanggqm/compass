"""Authorized input references and code bounds for the Python sandbox tool."""

from compass_agent.prompt_assets import tool_text
from compass_agent.schemas import ToolDefinition, ToolFunction
from compass_agent.tools.builtin.python_analysis.evidence import calculation
from compass_agent.tools.catalog_contracts import ToolSpec
from compass_agent.tools.contracts import (
    RegisteredTool,
    SandboxBinding,
    ToolPolicy,
    ToolRejected,
    json_bytes,
)
from compass_agent.tools.registry import bounded_payload, parse_arguments


def fingerprint(args, context=None):
    from hashlib import sha256

    from compass_agent.llm.continuation import canonical

    source = context.sources.get(args.get("source_query_id")) if context else None
    if source is None or not isinstance(source.data, dict):
        return None
    data = {k: v for k, v in source.data.items() if k in {"columns", "rows"}}
    value = {"tool": "python_analysis", "code": args["code"], "input": data}
    return sha256(canonical(value)).hexdigest(), "effect"


def python_analysis_tool() -> RegisteredTool:
    def authorize(args, context):
        if not isinstance(args, dict) or not isinstance(args.get("code"), str):
            raise ToolRejected("invalid_parameters")
        source = context.sources.get(args.get("source_query_id"))
        if source is None or source.owner != context.owner or source.run_id != context.run_id:
            raise ToolRejected("sandbox_source_denied")
        if len(args["code"].encode()) > 16 * 1024 or len(json_bytes(source.data)) > 128 * 1024:
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
