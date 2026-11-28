"""Slim input contracts and structured repair messages."""

from compass_agent.tools.contracts import json_bytes
from compass_agent.tools.errors import ActionableToolError, invalid_arguments
from compass_agent.tools.schema_compact import compact_json_schema
from jsonschema import Draft202012Validator


def test_schema_compaction_preserves_named_properties_and_constraints():
    source = {
        "type": "object",
        "title": "Input",
        "properties": {
            "title": {"type": "string", "minLength": 1},
            "option": {"anyOf": [{"$ref": "#/$defs/Choice"}, {"type": "null"}], "default": None},
        },
        "$defs": {"Choice": {"type": "string", "enum": ["a", "b"]}},
    }
    result = compact_json_schema(source)
    assert "title" in result["properties"]
    assert result["properties"]["title"]["minLength"] == 1
    assert result["properties"]["option"]["enum"] == ["a", "b"]
    assert "$defs" not in result
    assert "$defs" in source


def test_schema_error_localizes_field_without_echoing_input():
    schema = {
        "type": "object",
        "properties": {"mode": {"enum": ["plain", "evidence"]}},
        "required": ["mode"],
    }
    missing = next(Draft202012Validator(schema).iter_errors({}))
    assert invalid_arguments(missing).error.payload()["field"] == "mode"
    error = next(Draft202012Validator(schema).iter_errors({"mode": "private-secret"}))
    payload = invalid_arguments(error).error.payload()
    assert payload["candidates"] == ["plain", "evidence"]
    assert "private-secret" not in str(payload)


def test_error_payload_is_bounded_for_multibyte_text():
    error = ActionableToolError(
        "x" * 200,
        "fix_arguments",
        "测" * 2000,
        field="列" * 300,
        candidates=tuple("𠮷" * 200 for _ in range(100)),
        next_action="𠮷" * 1000,
    )
    assert len(json_bytes(error.payload())) <= 4096


def test_large_original_cannot_be_exported_as_a_preview_only_table():
    import asyncio
    from types import SimpleNamespace

    import pytest
    from compass_agent.execution_context import current_execution
    from compass_agent.tools.builtin.final_answer import complete
    from compass_agent.tools.errors import ActionableToolRejected

    journal = SimpleNamespace(
        context_binding={"answerProtocol": 3},
        answer_evidence={
            "root": {"q": {"kind": "query", "record": {"ref": "original", "preview": [[1]]}}}
        },
    )
    token = current_execution.set(journal)
    try:
        with pytest.raises(ActionableToolRejected) as error:
            asyncio.run(complete({"mode": "evidence", "artifactFormat": "csv"}, None))
        assert error.value.error.code == "artifact_requires_bounded_rows"
        result = asyncio.run(complete({"mode": "evidence", "artifactFormat": "markdown"}, None))
        assert result.status == "ok"
    finally:
        current_execution.reset(token)


def test_subagent_step_allocation_preserves_existing_domain_budget():
    import pytest
    from compass_agent.tools.builtin.sub_agent import SubAgentInput
    from compass_agent.tools.builtin.sub_agent.model_schema import SCHEMA
    from compass_agent.tools.builtin.sub_agent.slim import normalize_input
    from jsonschema import ValidationError

    value = {"tasks": [{"name": "核对", "objective": "探查后取数并汇报", "maxSteps": 8}]}
    Draft202012Validator(SCHEMA).validate(value)
    task = SubAgentInput.model_validate(normalize_input(value, None))
    assert task.tasks and task.tasks[0].max_steps == 8
    value["tasks"][0]["maxSteps"] = 9
    with pytest.raises(ValidationError):
        Draft202012Validator(SCHEMA).validate(value)


def test_slim_todo_preserves_verified_items_and_rolls_back_invalid_transitions():
    import pytest
    from compass_agent.tools.builtin.todo_write.plan import ExecutionPlan, TodoRejected
    from compass_agent.tools.builtin.todo_write.slim import apply_slim

    plan = ExecutionPlan()
    apply_slim(
        plan,
        {
            "todos": [
                {"step": "取数", "status": "in_progress"},
                {"step": "解释", "status": "pending"},
            ]
        },
        1,
    )
    apply_slim(
        plan,
        {"todos": [{"step": "取数", "status": "done"}, {"step": "解释", "status": "in_progress"}]},
        2,
    )
    saved = plan.snapshot()
    with pytest.raises(TodoRejected):
        apply_slim(
            plan,
            {
                "todos": [
                    {"step": "取数", "status": "pending"},
                    {"step": "解释", "status": "in_progress"},
                ]
            },
            3,
        )
    assert plan.snapshot() == saved
    with pytest.raises(TodoRejected):
        apply_slim(
            plan,
            {
                "todos": [
                    {"step": "另取数据", "status": "pending"},
                    {"step": "解释", "status": "in_progress"},
                ]
            },
            3,
        )
    assert plan.snapshot() == saved
    apply_slim(
        plan, {"todos": [{"step": "取数", "status": "done"}, {"step": "解释", "status": "done"}]}, 4
    )
    assert all(status == "verified" for status in plan.status.values())


def test_schedule_shorthand_and_literal_cron():
    from compass_agent.tools.builtin.schedule_write.slim import compile_schedule_expr

    assert compile_schedule_expr("daily 09:00") == "0 9 * * *"
    assert compile_schedule_expr("weekly MON 09:00") == "0 9 * * 1"
    assert compile_schedule_expr("0 9 * * 1") == "0 9 * * 1"


def test_evidence_template_accepts_bounded_multiline_material_without_nested_render():
    from compass_agent.context_runtime.stream_answer import evidence_prompt
    from compass_agent.promptops.template import render

    material = evidence_prompt(
        [
            {
                "title": "已知结果",
                "locator": "查询",
                "record": {"text": "测试" * 300, "literal": "{{question}}"},
            }
        ]
    )
    value = render(
        "answer/evidence.md", "材料：{{ordered_evidences}}", {"ordered_evidences": material}
    )
    assert "{{question}}" in value and "<untrusted_context " in value


def test_large_answer_evidence_is_bounded_and_keeps_stable_ordinals():
    from compass_agent.context_runtime.stream_answer import evidence_prompt, ordered_evidence
    from compass_agent.promptops.template import render

    records = {
        str(i): {
            "id": str(i),
            "kind": "query",
            "title": "销售",
            "locator": "本月",
            "record": {"columns": ["销售额"], "rows": [["测" * 1000]] * 100},
        }
        for i in range(150)
    }
    ordered = ordered_evidence(records)
    assert len(ordered) == 128
    prompt = evidence_prompt(ordered)
    assert len(prompt.encode()) < 196608 and "previewTruncated" in prompt
    assert prompt == evidence_prompt(ordered_evidence(dict(reversed(list(records.items())))))
    assert render("answer/evidence.md", "{{ordered_evidences}}", {"ordered_evidences": prompt})
    assert len(ordered[0]["record"]["rows"]) == 100


def test_metric_query_result_is_an_authorized_python_input(monkeypatch):
    import asyncio
    from dataclasses import replace
    from types import SimpleNamespace
    from typing import Any, cast
    from unittest.mock import AsyncMock
    from uuid import uuid4

    import pytest
    from compass_agent.tools.builtin.python_analysis import python_analysis_tool
    from compass_agent.tools.business.query_metric import BASE
    from compass_agent.tools.catalog_contracts import CatalogContext
    from compass_agent.tools.contracts import InProcessBinding, ToolRejected

    from .test_tool_dispatcher import context

    checks = []
    monkeypatch.setattr(
        "compass_agent.context_runtime.source_access.require_run_sources",
        lambda *args, **kwargs: checks.append(args),
    )
    service = SimpleNamespace(
        run=AsyncMock(
            return_value={"resultId": "metric-1", "columns": ["amount"], "rows": [[300]]}
        ),
        store=object(),
        journal=SimpleNamespace(run_id=uuid4(), epoch=uuid4()),
    )
    assert BASE.context_factory
    entry = BASE.context_factory(CatalogContext(frozenset(), semantics=cast(Any, service)))
    assert isinstance(entry.binding, InProcessBinding)
    handler = entry.binding.handler

    async def scenario():
        execution = context("query_metric", "python_analysis")
        receipt = await handler({"query_ref": str(uuid4())}, execution)
        assert isinstance(receipt.payload, dict)
        assert receipt.payload["sourceQueryId"] == receipt.payload["resultId"] == "metric-1"
        source = execution.sources["metric-1"]
        assert source.revalidate is not None
        assert await source.revalidate() and len(checks) == 1
        python = python_analysis_tool()
        args = {"source_query_id": "metric-1", "code": "result = data['rows'][0][0]"}
        python.authorize(args, execution)
        with pytest.raises(ToolRejected, match="sandbox_source_denied"):
            python.authorize(args, replace(execution, run_id=uuid4()))

    asyncio.run(scenario())


def test_missing_semantic_binding_offers_authorized_calculation_without_permission_fallback():
    import asyncio
    from types import SimpleNamespace
    from typing import Any, cast
    from unittest.mock import AsyncMock

    from compass_agent.tools.business.resolve_semantics import SPEC
    from compass_agent.tools.catalog_contracts import CatalogContext
    from compass_agent.tools.contracts import InProcessBinding

    from .test_tool_dispatcher import context

    async def scenario():
        for status in ("unavailable_binding", "query_forbidden"):
            service = SimpleNamespace(
                run=AsyncMock(return_value={"status": status, "metric": {"name": "指标"}})
            )
            assert SPEC.context_factory is not None
            tool = SPEC.context_factory(CatalogContext(frozenset(), semantics=cast(Any, service)))
            assert isinstance(tool.binding, InProcessBinding)
            receipt = await tool.binding.handler({"mention": "指标"}, context())
            assert isinstance(receipt.payload, dict) and receipt.payload["status"] == status
            if status == "unavailable_binding":
                assert receipt.payload["category"] == "switch_tool"
                assert receipt.payload["suggestedTool"] == "database_query"
            else:
                assert "suggestedTool" not in receipt.payload

    asyncio.run(scenario())
