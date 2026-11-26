import json
from types import SimpleNamespace

import pytest
from compass_agent.context_runtime.pipeline import fit
from compass_agent.context_runtime.search_control import (
    SearchProgress,
    delivery_reserve,
    model_observation,
    parse_targets,
    remaining,
    search_messages,
)
from compass_agent.harness.autonomy import AutonomyBudget, AutonomyLimits
from compass_agent.harness.contracts import AgentState, ToolResult
from compass_agent.schemas import GenerateInput, ToolCall, ToolCallFunction

PIN = {
    "kinds": ["detail", "semantic"],
    "timezone": "Asia/Shanghai",
    "knownAt": "2025-10-09T09:00:00+08:00",
}


def call(identifier, name):
    return ToolCall(id=identifier, function=ToolCallFunction(name=name, arguments="{}"))


def plan(key="指标", source="semantic"):
    targets = parse_targets(
        json.dumps({"targets": [{"key": key, "source": source, "goal": "核对适用窗口"}]})
    )
    assert targets is not None
    return targets


def test_required_targets_keep_identity_source_and_necessity():
    progress = SearchProgress(PIN)
    assert progress.plan(plan())
    assert not progress.plan(plan(source="detail"))
    assert not progress.plan(plan(key="other"))
    assert set(progress.targets) == {"指标", "other"}
    assert progress.targets["指标"].source == "semantic"
    assert progress.plan([*progress.targets.values(), *plan(key="detail", source="detail")])
    assert progress.independent_detail()
    assert remaining(AutonomyBudget((), AutonomyLimits(tools=2)))["toolCalls"] == 2


def test_replayed_batch_is_idempotent_and_new_result_id_does_not_reset_progress():
    progress = SearchProgress(PIN)
    progress.plan(plan())
    budget = AutonomyBudget((), AutonomyLimits())
    state = AgentState([])
    for pos in range(3):
        step = state.start_step(pos + 1)
        current = call(f"call-{pos}", "query_metric")
        step.tool_calls = (current,)
        result = SimpleNamespace(
            call_id=current.id, status="ok", payload={"resultId": str(pos), "rows": [[1]]}
        )
        progress.record(step, [result], [], budget)
        progress.record(step, [result], [], budget)
    assert progress.stop_reason == "no_progress"
    assert progress.stalled == {"指标": 2}


def test_question_and_native_receipts_survive_context_compaction_together():
    progress = SearchProgress(PIN)
    state = AgentState([])
    for pos in range(2):
        step = state.start_step(pos + 1)
        current = call(f"call-{pos}", "query_metric")
        step.tool_calls = (current,)
        state.record_tool_result(
            step,
            ToolResult(current.id, "ok", json.dumps({"value": "x" * (2200 if not pos else 5)})),
        )
    questions = search_messages(
        progress, state, "本期实收多少？", "query", AutonomyBudget((), AutonomyLimits()), None
    )
    packed, *_ = fit(GenerateInput(messages=questions), 1600)
    assert [m.role for m in packed.messages] == ["user", "assistant", "tool"]
    assert packed.messages[0].content is not None
    assert "本期实收多少" in packed.messages[0].content
    assert packed.messages[-2].tool_calls is not None
    assert packed.messages[-1].tool_call_id == packed.messages[-2].tool_calls[0].id


def test_final_generation_reserve_checks_price_and_remaining_time():
    from uuid import uuid4

    from compass_agent.settings import AutonomyModelProfile

    model = AutonomyModelProfile(
        provider_id=uuid4(),
        binding_version=1,
        sdk_model="test/model",
        api_base="",
        input_tokens=100,
        output_tokens=20,
        input_microusd=2,
        output_microusd=5,
    )
    budget = AutonomyBudget(
        (model,), AutonomyLimits(steps=10, tools=5, tokens=250, cost_microusd=605)
    )
    route = SimpleNamespace(provider_id=model.provider_id, binding_version=1)
    assert not delivery_reserve(budget, route, 50)
    assert delivery_reserve(budget, route, 20)
    budget.cost_microusd = 10
    assert delivery_reserve(budget, route, 50)
    budget.cost_microusd = 0
    budget.tokens = 11
    assert delivery_reserve(budget, route, 50)


def test_private_rebuild_checks_each_source_set_and_checks_live_again_before_new_work(monkeypatch):
    from compass_agent.context_runtime import source_access
    from compass_agent.models.errors import ModelError

    context = SimpleNamespace(model_dump=lambda **_: {"actor": "reader"})
    journal = SimpleNamespace(store=None, rebuilding_search=True, rebuilt_source_digest=None)
    calls = []

    def allowed(store, actor, refs, **kw):
        calls.append(tuple(r["kind"] for r in refs))
        return len(calls) < 3

    monkeypatch.setattr(source_access, "allows", allowed)
    for refs in (
        [{"kind": "document"}],
        [{"kind": "document"}],
        [{"kind": "document"}, {"kind": "semantic"}],
    ):
        source_access.require_search_sources(journal, context, refs=refs)
    journal.rebuilding_search = False
    with pytest.raises(ModelError, match="source_access_denied"):
        source_access.require_search_sources(journal, context, refs=refs)
    assert len(calls) == 3


def test_projection_keeps_scope_and_incomplete_metric_rows():
    payload = {
        "unit": "count",
        "time_range": [{"start": "2026-01-01", "end": "2026-02-01"}],
        "columns": ["value", "status", "detail", "unit"],
        "rows": [[None, "NO_FACTS", "缺样本", "count"]],
    }
    visible = model_observation("query_metric", payload)
    assert visible["rows"] == [[None, "NO_FACTS", "缺样本"]]
    assert visible["time_range"] == payload["time_range"]
