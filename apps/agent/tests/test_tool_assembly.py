"""Lesson 27 supply boundaries, independent of handlers or a running provider."""

from dataclasses import replace
from hashlib import sha256
from time import perf_counter
from uuid import UUID, uuid4

import pytest
from compass_agent.schemas import ToolDefinition, ToolFunction, ToolSelection
from compass_agent.tools.assembler import AssemblyInput, assemble_step
from compass_agent.tools.catalog import TOOLS
from compass_agent.tools.catalog_contracts import CatalogContext
from compass_agent.tools.contracts import (
    ExecutionReceipt,
    InProcessBinding,
    RegisteredTool,
    ToolGovernance,
    ToolPolicy,
    ToolRejected,
    json_bytes,
)
from compass_agent.tools.registry import (
    ToolRegistry,
    bounded_payload,
    parse_arguments,
    validate_catalog,
)

CORE = {
    "final_answer",
    "read_tool_result",
    "search_semantics",
    "get_metric_definition",
    "resolve_semantics",
    "query_metric",
    "database_query",
    "search_documents",
    "read_document",
    "schedule_read",
    "schedule_write",
}
DEEP = {"todo_write", "python_analysis", "sub_agent"}


async def noop(*_):
    return ExecutionReceipt("test", "ok", "confirmed")


def unavailable_proposal(*_):
    raise ToolRejected("approval_required")


def entries():
    context = CatalogContext(frozenset(), database=True, sandbox=True, workflow=True)
    return {
        spec.name: RegisteredTool(
            spec.name,
            spec.definition(context),
            ToolPolicy("external_write", "required")
            if spec.name == "schedule_write"
            else ToolPolicy(),
            InProcessBinding(noop),
            parse_arguments,
            lambda *_: None,
            bounded_payload,
            governance=spec.governance,
            completion=spec.name == "final_answer",
            prepare_proposal=unavailable_proposal if spec.name == "schedule_write" else None,
        )
        for spec in TOOLS
    }


def extension(name, *, workflow_id=None, mcp_id=None):
    return RegisteredTool(
        name,
        ToolDefinition(
            function=ToolFunction(name=name, description="扩展", parameters={"type": "object"})
        ),
        ToolPolicy(),
        InProcessBinding(noop),
        parse_arguments,
        lambda *_: None,
        bounded_payload,
        governance=ToolGovernance(),
        workflow_id=workflow_id,
        mcp_server_id=mcp_id,
    )


def context(**changes):
    return replace(
        AssemblyInput("root", "query", "SC-01", "query", is_semantic_maintainer=True), **changes
    )


def test_required_capabilities_are_independent_of_scene_but_obey_permissions():
    pool = entries()
    requested = frozenset({"python_analysis", "create_workflow", "review_semantic_change"})
    result = assemble_step(pool, context(required_tools=requested, is_semantic_maintainer=False))
    names = {item.function.name for item in result.definitions}
    assert {"python_analysis", "create_workflow"} <= names
    assert "review_semantic_change" not in names
    child = assemble_step(pool, context(required_tools=requested, caller_role="subagent"))
    assert "create_workflow" not in {item.function.name for item in child.definitions}
    answer = assemble_step(pool, context(required_tools=requested, phase="answer_stream"))
    assert not answer.definitions


def names(result):
    return {d.function.name for d in result.definitions}


@pytest.mark.parametrize(
    "scene,expected", [("SC-01", CORE), ("SC-21", CORE), ("SC-03", CORE | DEEP)]
)
def test_scene_supply(scene, expected):
    result = assemble_step(entries(), context(scene_id=scene))
    assert names(result) == expected
    assert not expected.intersection(result.deferred_pool)
    assert all(x.channel == "always_on" for x in result.trace.exposed if x.name in CORE)


def test_manual_selection_and_unauthorized_ids():
    pool = entries()
    owner, stranger = uuid4(), uuid4()
    pool["workflow_own"] = extension("workflow_own", workflow_id=owner)
    pool["workflow_other"] = extension("workflow_other", workflow_id=uuid4())
    selected = context(
        selected_packs=frozenset({"workflow_pack", "semantic_govern"}),
        selected_workflow_ids=frozenset({owner, stranger}),
        is_semantic_maintainer=False,
    )
    result = assemble_step(pool, selected)
    assert names(result) == CORE | {"create_workflow", "workflow_own"}
    assert result.trace.permission_filtered_count == 3
    assert not any("semantic_change" in n for n in result.deferred_pool)
    assert "discover_semantic_candidates" not in str(result.trace)


def test_large_pool_and_extension_budget():
    pool = entries()
    for i in range(30):
        pool[f"mcp_{i:02}"] = extension(f"mcp_{i:02}", mcp_id=UUID(int=1))
    registry = ToolRegistry(pool.values())
    assert len(registry.entries) == 49
    result = assemble_step(pool, context(selected_mcp_server_ids=frozenset({UUID(int=1)})))
    assert len(names(result) - CORE) == 12
    assert len(result.deferred_pool) == 26
    validate_catalog(result.definitions)
    assert result.trace.declaration_bytes == len(
        json_bytes([d.model_dump() for d in result.definitions])
    )
    with pytest.raises(ToolRejected, match="tool_catalog_limit"):
        validate_catalog(registry.definitions())
    for i in range(49, 129):
        pool[f"extra_{i}"] = extension(f"extra_{i}")
    with pytest.raises(ToolRejected, match="tool_registry_pool_limit"):
        ToolRegistry(pool.values())


def test_role_and_stage_boundaries():
    pool = entries()
    assert "schedule_write" not in names(assemble_step(pool, context(caller_role="scheduler")))
    child = assemble_step(
        pool, context(caller_role="subagent", selected_packs=frozenset({"semantic_govern"}))
    )
    assert not names(child).intersection(
        {"schedule_write", "final_answer", "goal", "sub_agent", "create_workflow"}
    )
    for phase in ("identify", "answer_stream"):
        assert not assemble_step(pool, context(phase=phase)).definitions
    assert names(assemble_step(pool, context(delivery_only=True))) == {"final_answer"}


def test_constant_work_prefix_and_latency():
    pool, frozen = entries(), context(scene_id="SC-03")
    hashes = []
    start = perf_counter()
    for _ in range(100):
        result = assemble_step(pool, frozen)
        hashes.append(sha256(json_bytes([d.model_dump() for d in result.definitions])).hexdigest())
    assert len(set(hashes)) == 1
    assert (perf_counter() - start) / 100 < 0.002


def test_selection_round_trip_and_bounds():
    selection = ToolSelection.model_validate_json(
        '{"packs":["long_task"],"workflowIds":[],"mcpServerIds":[]}'
    )
    assert ToolSelection.model_validate_json(selection.model_dump_json(by_alias=True)) == selection
    selected = ToolSelection(packs=["long_task"], workflowIds=[uuid4()], mcpServerIds=[uuid4()])
    assert ToolSelection.model_validate(selected.model_dump(mode="json", by_alias=True)) == selected
    with pytest.raises(ValueError):
        ToolSelection.model_validate({"packs": ["injected_admin"]})
    for invalid in ("not-a-uuid", 1, True):
        with pytest.raises(ValueError):
            ToolSelection.model_validate({"workflowIds": [invalid]})


@pytest.mark.parametrize("count,description_size", [(32, 20), (30, 2400)])
def test_child_report_shares_the_slot_and_byte_budget(monkeypatch, count, description_size):
    from types import SimpleNamespace

    from compass_agent.context_runtime import tool_assembly
    from compass_agent.execution_context import execution_path
    from compass_agent.goal.report_schema import SCHEMA
    from compass_agent.schemas import ChatMessage, GenerateInput

    monkeypatch.setattr(tool_assembly, "read_private", lambda *_: {})
    pool = {}
    for i in range(count):
        item = extension(f"mcp_read_{i:02}")
        assert item.definition
        item.definition.function.description = "x" * description_size
        pool[item.name] = replace(
            item, governance=ToolGovernance(channel="always_on", subagent_allowed=True)
        )
    report = ToolDefinition(
        function=ToolFunction(name="report_to_parent", description="交回结论", parameters=SCHEMA)
    )
    draft = GenerateInput(messages=[ChatMessage(role="user", content="分析结果")]).model_copy(
        update={"tools": [*[item.definition for item in pool.values()], report]}
    )
    journal = SimpleNamespace(store=None, run_id=uuid4(), tool_selection={}, tool_entries=pool)
    ctx = SimpleNamespace(scope_profile="tenant", task="query", scene="SC-03", phase="query")
    token = execution_path.set("root/subagent:budget")
    try:
        result, trace = tool_assembly.assemble(journal, None, ctx, draft, {"plugins": True})
        validate_catalog(result.tools)
        assert result.tools[0].function.name == "report_to_parent"
        assert len(result.tools) < len(draft.tools)
        assert trace["exposed_count"] == len(result.tools)
        assert trace["declaration_bytes"] == len(json_bytes([t.model_dump() for t in result.tools]))
        ctx.phase = "identify"
        assert not tool_assembly.assemble(journal, None, ctx, draft, {"plugins": True})[0].tools
    finally:
        execution_path.reset(token)
