"""Capacity boundaries and dependency visibility for lesson 24's actual modules."""

import asyncio
import json
from types import SimpleNamespace
from uuid import uuid4

from compass_agent.context_runtime.compaction import recent_turns, summarize
from compass_agent.context_runtime.history import TurnGroup
from compass_agent.context_runtime.selection import fit_batch, select_history
from compass_agent.schemas import CallResponse, ChatMessage, DecisionAnswer, ModelTokenLimits


def group(n, content=None):
    run = uuid4()
    return TurnGroup(
        str(n),
        run,
        n,
        (
            ChatMessage(role="user", content=content or f"问题 {n}"),
            ChatMessage(role="assistant", content=f"答复 {n}"),
        ),
        (f"{n}/user", f"{n}/answer"),
    )


class Journal:
    def __init__(self):
        self.run_id = uuid4()
        self.last_attempt = None
        self.context_binding = {"generation": "test"}
        self.provider = uuid4()
        self.context_results = SimpleNamespace(dependencies=lambda: [])
        self.store = SimpleNamespace(
            source_settings=SimpleNamespace(compass_routing_jev_provider_id=self.provider)
        )

    def prefetch_prompts(self, paths):
        pass

    def prompt(self, path):
        return "保留当前任务、跨轮依赖、纠正和已执行动作；不确定时保留。"


def setup():
    journal = Journal()
    models = SimpleNamespace(
        repository=SimpleNamespace(get=lambda _: SimpleNamespace(binding_version=1))
    )
    control = SimpleNamespace(deadline_at=float("inf"))
    budget = SimpleNamespace(
        profiles=[
            SimpleNamespace(
                provider_id=journal.provider,
                binding_version=1,
                input_tokens=100_000,
                output_tokens=2048,
            )
        ]
    )
    return journal, models, control, budget


def reply(journal, request, choices=None, text=""):
    journal.last_attempt = uuid4()
    return CallResponse(
        call_id=uuid4(),
        provider_id=journal.provider,
        config_version=1,
        status="completed",
        duration_ms=1,
        requested_model="test",
        kind=request.kind,
        finish_reason="stop",
        text=text,
        answers={
            k: DecisionAnswer(
                type="choice",
                choice=(choices or {}).get(k, "keep"),
                probabilities={
                    "keep": 0.99 if (choices or {}).get(k, "keep") == "keep" else 0.01,
                    "drop": 0.01 if (choices or {}).get(k, "keep") == "keep" else 0.99,
                },
            )
            for k in request.questions
        }
        if request.kind == "decision"
        else None,
    )


def test_all_batches_share_dependency_background_and_protected_turns(monkeypatch):
    journal, models, control, budget = setup()
    groups = [group(n) for n in range(1, 22)]
    requests = []

    async def call(*args, request, **kwargs):
        requests.append(request)
        return reply(journal, request, {str(n): "drop" for n in range(1, 19) if n not in (2, 17)})

    monkeypatch.setattr("compass_agent.context_runtime.selection.auxiliary_call", call)
    result = asyncio.run(
        select_history(
            journal,
            models,
            None,
            control,
            budget,
            groups,
            "沿用第 17 轮的口径",
            pressured=lambda _: True,
        )
    )
    assert [len(r.questions) for r in requests] == [16, 2]
    assert requests[0].state == requests[1].state
    assert [g["groupId"] for g in requests[0].state["candidates"]] == [str(n) for n in range(1, 19)]
    assert [g.id for g in result.retained] == ["2", "17", "19", "20", "21"]
    assert not result.snapshot()["unjudged"]


def test_stop_retains_unjudged_and_request_overflow_sends_nothing(monkeypatch):
    journal, models, control, budget = setup()
    groups = [group(n) for n in range(21)]
    requests = []

    async def call(*args, request, **kwargs):
        requests.append(request)
        return reply(journal, request, {key: "drop" for key in request.questions})

    monkeypatch.setattr("compass_agent.context_runtime.selection.auxiliary_call", call)
    result = asyncio.run(
        select_history(
            journal,
            models,
            None,
            control,
            budget,
            groups,
            "当前问题",
            pressured=lambda retained: len(retained) > 8,
        )
    )
    assert len(requests) == 1
    assert result.snapshot()["unjudged"] == ["16", "17"]
    assert [g.id for g in result.retained] == ["16", "17", "18", "19", "20"]
    budget.profiles[0].input_tokens = 128
    requests.clear()
    result = asyncio.run(
        select_history(
            journal, models, None, control, budget, groups, "另一问题", pressured=lambda _: True
        )
    )
    assert not requests and len(result.retained) == 21
    assert result.reason == "selection_input_too_large"


def test_fit_reduces_questions_without_shrinking_state():
    state = {"candidates": ["跨轮事实" * 200]}
    request = fit_batch(state, [str(i) for i in range(16)], 6000)
    assert request and request.state == state and len(request.questions) < 16
    assert fit_batch(state, ["a"], 40) is None


def test_recent_suffix_is_contiguous_and_whole():
    groups = [group(1), group(2, "字" * 2000), group(3)]
    assert [g.id for g in recent_turns(groups, {"1", "2", "3"}, 1000)] == ["3"]
    assert recent_turns(groups, {"1", "2"}, 10000) == []
    assert recent_turns([group(1, "字" * 2000)], {"1"}, 1000) == []


def test_summary_chunks_original_ranges_and_rebuilds_when_selection_changes(monkeypatch):
    journal, models, control, budget = setup()
    route = SimpleNamespace(provider_id=journal.provider, binding_version=1)
    groups = [group(1, "退款 R-071 314.80元 陈岚 下一工作日。" + "上下文" * 2500), group(2)]
    requests = []

    async def call(*args, request, **kwargs):
        requests.append(request)
        return reply(journal, request, text="退款 R-071；314.80元；陈岚；下一工作日。")

    monkeypatch.setattr("compass_agent.context_runtime.compaction.auxiliary_call", call)
    limits = ModelTokenLimits(max_input_tokens=5000, max_output_tokens=2048)
    summary = asyncio.run(
        summarize(journal, models, None, control, budget, route, limits, groups, "跟进退款")
    )
    assert len(requests) > 1
    assert {r.split(":")[0] for r in summary.covered_ranges} == {
        "1/user",
        "1/answer",
        "2/user",
        "2/answer",
    }
    seen = [x for r in requests for x in json.loads(r.messages[1].content)["selectedHistory"]]
    fragments = [x for x in seen if x["range"].startswith("1/user:")]
    original = "".join(x["fragment"] for x in fragments)
    assert json.loads(original)["content"] == groups[0].messages[0].content
    requests.clear()
    new = asyncio.run(
        summarize(
            journal,
            models,
            None,
            control,
            budget,
            route,
            limits,
            [groups[0]],
            "跟进退款",
            previous=summary,
        )
    )
    assert requests and json.loads(requests[0].messages[1].content)["previousSummary"] == ""
    assert new.selected_ids == ("1",)
    requests.clear()
    restored = asyncio.run(
        summarize(
            journal, models, None, control, budget, route, limits, groups, "跟进退款", previous=new
        )
    )
    assert set(restored.covered_ranges) == set(summary.covered_ranges)
    assert all(
        x["range"].startswith("2/")
        for r in requests
        for x in json.loads(r.messages[1].content)["selectedHistory"]
    )


def test_real_systemone_executor_accounts_optional_decision_and_preserves_main_budget():
    from dataclasses import replace

    import httpx
    from compass_agent.context_runtime.optimization_calls import OptionalBudget
    from compass_agent.context_runtime.selection import decision_request
    from compass_agent.harness.autonomy import AutonomyBudget, AutonomyLimits
    from compass_agent.llm.admission import ExecutionGate
    from compass_agent.llm.executor import ReliableExecutor
    from compass_agent.llm.policy import LogicalPolicy
    from compass_agent.llm.runtime import Runtime
    from compass_agent.llm.systemone_decision import SystemOneDecisionAdapter
    from compass_agent.settings import AutonomyModelProfile

    from .test_runtime import request

    async def scenario():
        req = request(5)
        spec = replace(
            req.access.spec,
            vendor="typesafe",
            kind="decision",
            adapter="systemone_decision",
            api_model="jev-latest",
            sdk_model=None,
        )
        req = replace(
            req,
            access=replace(req.access, spec=spec),
            input=decision_request({"candidate": "irrelevant"}, ["old"]),
        )
        profile = AutonomyModelProfile(
            provider_id=spec.provider_id,
            binding_version=spec.binding_version,
            sdk_model=spec.sdk_model,
            api_base=spec.api_base,
            input_tokens=4096,
            output_tokens=1024,
            input_microusd=2,
            output_microusd=3,
        )
        calls = []

        async def respond(incoming):
            calls.append(json.loads(incoming.content))
            return httpx.Response(
                200,
                json={
                    "answers": {
                        "old": {
                            "type": "choice",
                            "choice": "drop",
                            "probabilities": {"keep": 0.01, "drop": 0.99},
                        }
                    },
                    "usage": {"input_tokens": 100, "output_tokens": 20},
                },
            )

        runner = ReliableExecutor(
            Runtime(
                {
                    "systemone_decision": SystemOneDecisionAdapter(
                        lambda: httpx.MockTransport(respond)
                    )
                }
            ),
            ExecutionGate(),
        )
        ledger = AutonomyBudget((profile,), AutonomyLimits())
        result = await runner.execute(
            req, LogicalPolicy(max_attempts=1), attempt_budget=OptionalBudget(ledger)
        )
        assert (
            result.status == "completed" and ledger.tokens == 120 and ledger.cost_microusd == 260
        ), (result.error, ledger.reason, calls)
        assert ledger.steps == 0 and ledger.reason is None
        assert list(calls[0]) == ["model", "state", "questions"]
        # Optional admission refusal consumes no provider request and does not stop the main task.
        denied = AutonomyBudget((profile,), AutonomyLimits(tokens=1))
        response = await runner.execute(
            req, LogicalPolicy(max_attempts=1), attempt_budget=OptionalBudget(denied)
        )
        assert response.status == "failed" and denied.reason is None and len(calls) == 1

    asyncio.run(scenario())


def optimizer_frame(journal, messages, **budget_extra):
    from compass_agent.context_runtime.optimizer import ContextOptimizer
    from compass_agent.schemas import GenerateInput

    journal.user_question = "当前问题"
    journal.context_accounted_attempts = set()
    journal.context_results = SimpleNamespace(dependencies=lambda: [])
    optimizer = ContextOptimizer(journal)
    optimizer.groups = []
    draft = GenerateInput(messages=messages, thinking=True, max_tokens=512)
    frame = {
        "input": {"request": draft.model_dump(mode="json", exclude={"continuation_token"})},
        "receipt": {"stepKey": "root/model:step-1", "budget": budget_extra},
        "continuation": None,
    }
    return optimizer, draft, frame


def test_optimizer_preserves_compiled_confirmation_and_text_only_thinking():
    journal, models, control, _ = setup()
    question = ChatMessage(role="user", content="当前问题")
    optimizer, draft, frame = optimizer_frame(
        journal, [ChatMessage(role="system", content="固定系统"), question]
    )
    frame["input"]["request"]["messages"][-1]["content"] += "\n用户已确认：退款按入账时间统计。"
    optimizer.groups = [group(1)]
    result = asyncio.run(
        optimizer.optimize(
            frame,
            models,
            None,
            control,
            None,
            None,
            ModelTokenLimits(max_input_tokens=100000, max_output_tokens=2048),
            draft,
        )
    )
    request = result["input"]["request"]
    assert request["thinking"] is True
    assert request["messages"][-1]["content"].endswith("退款按入账时间统计。")
    assert request["messages"][0]["content"] == "固定系统"


def test_native_and_schema_overhead_participate_in_hard_capacity():
    import pytest
    from compass_agent.harness.context import ContextLimit

    for field in ("continuationExtra", "protocolExtra"):
        journal, models, control, _ = setup()
        optimizer, draft, frame = optimizer_frame(
            journal,
            [
                ChatMessage(role="system", content="固定"),
                ChatMessage(role="user", content="当前问题"),
            ],
            **{field: 5000},
        )
        if field == "continuationExtra":
            frame["continuation"] = {"sealedPrefix": "opaque"}
        with pytest.raises(ContextLimit):
            asyncio.run(
                optimizer.optimize(
                    frame,
                    models,
                    None,
                    control,
                    None,
                    None,
                    ModelTokenLimits(max_input_tokens=4000, max_output_tokens=2048),
                    draft,
                )
            )


def test_plain_tool_prefix_keeps_its_mode_after_continuation_and_restore():
    from copy import deepcopy

    from compass_agent.context_runtime.optimizer import ContextOptimizer
    from compass_agent.schemas import ToolCall, ToolCallFunction

    journal, models, control, _ = setup()
    messages = [
        ChatMessage(role="system", content="固定系统"),
        ChatMessage(role="user", content="当前问题"),
        ChatMessage(
            role="assistant",
            tool_calls=[ToolCall(id="c1", function=ToolCallFunction(name="read", arguments="{}"))],
        ),
        ChatMessage(role="tool", tool_call_id="c1", content="已读取"),
    ]
    optimizer, draft, frame = optimizer_frame(journal, messages)
    frame["input"]["request"]["thinking"] = False
    limits = ModelTokenLimits(max_input_tokens=100000, max_output_tokens=2048)

    async def scenario():
        first = await optimizer.optimize(frame, models, None, control, None, None, limits, draft)
        assert first["contextOptimization"]["plainToolPrefix"]
        restored = ContextOptimizer(journal)
        restored.groups = []
        restored.restore(first["contextOptimization"], messages)
        following = deepcopy(first)
        following["input"]["request"]["thinking"] = True
        following["continuation"] = {"native": "accepted plain-tool continuation"}
        second = await restored.optimize(
            following, models, None, control, None, None, limits, draft
        )
        assert second["input"]["request"]["thinking"] is False
        assert second["continuation"] == following["continuation"]
        assert second["input"]["request"]["messages"] == first["input"]["request"]["messages"]

    asyncio.run(scenario())


def test_current_closed_batches_are_summarized_and_pending_batch_stays_paired(monkeypatch):
    from compass_agent.context_runtime.compaction import SummarySnapshot
    from compass_agent.schemas import ToolCall, ToolCallFunction

    journal, models, control, _ = setup()
    messages = [
        ChatMessage(role="system", content="固定系统"),
        ChatMessage(role="user", content="当前问题"),
    ]
    for i in range(3):
        messages += [
            ChatMessage(
                role="assistant",
                tool_calls=[
                    ToolCall(id=f"c{i}", function=ToolCallFunction(name="read", arguments="{}"))
                ],
            ),
            ChatMessage(
                role="tool", tool_call_id=f"c{i}", content="字" * 2000 if i < 2 else "最新结果"
            ),
        ]
    optimizer, draft, frame = optimizer_frame(journal, messages)
    captured = []

    async def compact(*args, **kwargs):
        groups = args[7]
        captured.extend(groups)
        return SummarySnapshot(
            "已完成前两步，继续最新结果。",
            tuple(g.id for g in groups),
            tuple(r for g in groups for r in g.ranges),
            "q",
            "v",
            (),
            (),
        )

    monkeypatch.setattr("compass_agent.context_runtime.optimizer.summarize", compact)
    result = asyncio.run(
        optimizer.optimize(
            frame,
            models,
            None,
            control,
            None,
            None,
            ModelTokenLimits(max_input_tokens=4000, max_output_tokens=2048),
            draft,
        )
    )
    assert len(captured) == 1 and len(captured[0].messages) == 4
    rows = result["input"]["request"]["messages"]
    assert [m.get("tool_call_id") for m in rows if m["role"] == "tool"] == ["c2"]
    assert rows[-2]["tool_calls"][0]["id"] == "c2"
    assert result["contextOptimization"]["activeIds"][1].startswith("root/model:step-1/")


def test_changed_content_at_same_range_rebuilds_summary(monkeypatch):
    journal, models, control, budget = setup()
    route = SimpleNamespace(provider_id=journal.provider, binding_version=1)
    original = group(1, "R-071 314.80元")
    calls = []

    async def call(*args, request, **kwargs):
        calls.append(json.loads(request.messages[1].content))
        return reply(journal, request, text="对应材料的交接摘要")

    monkeypatch.setattr("compass_agent.context_runtime.compaction.auxiliary_call", call)
    limits = ModelTokenLimits(max_input_tokens=10000, max_output_tokens=2048)
    first = asyncio.run(
        summarize(journal, models, None, control, budget, route, limits, [original], "问题")
    )
    from dataclasses import replace

    changed = replace(
        original,
        messages=(ChatMessage(role="user", content="更正：金额 300.00元"), original.messages[1]),
    )
    asyncio.run(
        summarize(
            journal, models, None, control, budget, route, limits, [changed], "问题", previous=first
        )
    )
    assert calls[-1]["previousSummary"] == ""
    assert "300.00" in json.dumps(calls[-1], ensure_ascii=False)


def test_new_observation_reselects_dropped_history(monkeypatch):
    journal, models, control, budget = setup()
    groups = [group(n) for n in range(8)]
    seen = []

    async def call(*args, request, **kwargs):
        seen.append(request)
        return reply(
            journal, request, {} if request.state["currentObservations"] else {"0": "drop"}
        )

    monkeypatch.setattr("compass_agent.context_runtime.selection.auxiliary_call", call)
    first = asyncio.run(select_history(journal, models, None, control, budget, groups, "继续分析"))
    assert first.dropped == {"0"}
    second = asyncio.run(
        select_history(
            journal,
            models,
            None,
            control,
            budget,
            groups,
            "继续分析",
            previous=first,
            pressured=lambda _: True,
            active=[ChatMessage(role="assistant", content="新查询结果要求沿用第 0 轮的口径")],
        )
    )
    assert not second.dropped and len(seen) == 2
    assert first.fingerprint != second.fingerprint


def test_closed_history_keeps_tool_provenance_without_native_signatures():
    from compass_agent.context_runtime.history import historical_messages
    from compass_agent.harness.context import validate_messages
    from compass_agent.schemas import ToolCall, ToolCallFunction

    original = group(1)
    original = TurnGroup(
        original.id,
        original.run_id,
        1,
        (
            original.messages[0],
            ChatMessage(
                role="assistant",
                tool_calls=[
                    ToolCall(
                        id="historic-call",
                        function=ToolCallFunction(name="database_query", arguments="{}"),
                    )
                ],
            ),
            ChatMessage(role="tool", tool_call_id="historic-call", content='{"amount":314.80}'),
            original.messages[1],
        ),
        ("u", "c", "t", "a"),
    )
    projected = list(historical_messages([original], project_tools=True))
    validate_messages(projected)
    assert all(m.role != "tool" and not m.tool_calls for m in projected)
    assert projected[2].content
    assert "historic-call" in projected[2].content and "314.80" in projected[2].content


def test_preview_pruning_preserves_recovery_reference_and_original_hash():
    from compass_agent.context_runtime.compaction import fragments

    ref = "results/r_test.jsonl"
    original = group(1)
    body = json.dumps(
        {"status": "ok", "ref": ref, "returnedRows": 10000, "preview": list(range(50))}
    )
    material = TurnGroup(
        original.id,
        original.run_id,
        1,
        (
            ChatMessage(role="tool", tool_call_id="a", content=body),
            ChatMessage(role="tool", tool_call_id="b", content=body),
        ),
        ("a", "b"),
    )
    before = list(fragments(material))
    after = list(fragments(material, {ref}, set()))
    assert [m["range"] for m in before] == [m["range"] for m in after]
    raw = json.dumps(after, ensure_ascii=False)
    assert len(raw) < len(json.dumps(before, ensure_ascii=False))
    assert "10000" in raw and ref in raw


def test_summary_removes_tail_when_original_turn_exceeds_message_limit(monkeypatch):
    from compass_agent.context_runtime.compaction import SummarySnapshot
    from compass_agent.schemas import ToolCall, ToolCallFunction

    journal, models, control, _ = setup()
    question = ChatMessage(role="user", content="当前问题")
    optimizer, draft, frame = optimizer_frame(
        journal, [ChatMessage(role="system", content="系统"), question]
    )
    messages = [question]
    for n in range(256):
        messages.extend(
            [
                ChatMessage(
                    role="assistant",
                    tool_calls=[
                        ToolCall(id=str(n), function=ToolCallFunction(name="query", arguments="{}"))
                    ],
                ),
                ChatMessage(role="tool", tool_call_id=str(n), content="完成"),
            ]
        )
    messages.append(ChatMessage(role="assistant", content="已结束"))
    g = group(1)
    optimizer.groups = [
        TurnGroup(g.id, g.run_id, 1, tuple(messages), tuple(map(str, range(len(messages)))))
    ]

    async def summary(*args, **kwargs):
        return SummarySnapshot("已有工作已完成。", ("1",), (), "q", "p", (), ())

    monkeypatch.setattr("compass_agent.context_runtime.optimizer.summarize", summary)
    result = asyncio.run(
        optimizer.optimize(
            frame,
            models,
            None,
            control,
            None,
            None,
            ModelTokenLimits(max_input_tokens=1000000, max_output_tokens=2048),
            draft,
        )
    )
    assert len(result["input"]["request"]["messages"]) == 3
    assert result["receipt"]["contextOptimization"]["compacted"]
    assert result["receipt"]["contextOptimization"]["tail"] == 0


def long_tool_history():
    from compass_agent.schemas import ToolCall, ToolCallFunction

    messages = [ChatMessage(role="user", content="经营诊断，继续未完成工作")]
    for n in range(29):
        messages.extend(
            [
                ChatMessage(
                    role="assistant",
                    tool_calls=[
                        ToolCall(id=str(n), function=ToolCallFunction(name="query", arguments="{}"))
                    ],
                ),
                ChatMessage(role="tool", tool_call_id=str(n), content=f"结果{n}：" + "明细" * 1100),
            ]
        )
    return TurnGroup("history", uuid4(), 1, tuple(messages), tuple(f"r/{n}" for n in range(59)))


def test_large_59_fragment_history_compacts_with_bounded_2048_token_merges(monkeypatch):
    from compass_agent.context_runtime.compaction import request_size

    journal, models, control, budget = setup()
    optimizer, draft, frame = optimizer_frame(
        journal,
        [
            ChatMessage(role="system", content="固定规则" * 2500),
            ChatMessage(role="user", content="当前问题"),
        ],
    )
    original = long_tool_history()
    optimizer.groups = [original]
    requests = []
    seen = set()

    async def call(*args, request, **kwargs):
        requests.append(request)
        material = json.loads(request.messages[1].content)
        seen.update(x["range"] for x in material["selectedHistory"])
        assert request.max_tokens == 2048
        assert request_size(request) <= 2048 * 12
        return reply(journal, request, text="已完成查询；尚待归因及图表。回读原始 r/0 至 r/58。")

    monkeypatch.setattr("compass_agent.context_runtime.compaction.auxiliary_call", call)
    result = asyncio.run(
        optimizer.optimize(
            frame,
            models,
            None,
            control,
            budget,
            SimpleNamespace(provider_id=journal.provider, binding_version=1),
            ModelTokenLimits(max_input_tokens=262144, max_output_tokens=2048),
            draft,
        )
    )
    assert len(requests) > 1
    assert len(seen) == 59
    assert result["receipt"]["contextOptimization"]["compacted"]
    assert result["receipt"]["budget"]["after"] <= 192 * 1024
    assert (original.messages[-1].content or "").endswith("明细" * 1100)


def test_truncated_summary_retries_smaller_original_material_without_using_partial(monkeypatch):
    journal, models, control, budget = setup()
    requests = []
    seen = set()

    async def call(*args, request, **kwargs):
        material = json.loads(request.messages[1].content)
        requests.append(material)
        assert "截断摘要不可复用" not in material["previousSummary"]
        if len(requests) == 1:
            return reply(journal, request, text="截断摘要不可复用").model_copy(
                update={
                    "status": "incomplete",
                    "finish_reason": "length",
                    "error_code": "output_truncated",
                }
            )
        seen.update(x["range"] for x in material["selectedHistory"])
        return reply(journal, request, text="完整交接，明细回读原始记录。")

    monkeypatch.setattr("compass_agent.context_runtime.compaction.auxiliary_call", call)
    result = asyncio.run(
        summarize(
            journal,
            models,
            None,
            control,
            budget,
            SimpleNamespace(provider_id=journal.provider, binding_version=1),
            ModelTokenLimits(max_input_tokens=262144, max_output_tokens=2048),
            [long_tool_history()],
            "继续",
        )
    )
    assert len(requests[1]["selectedHistory"]) < len(requests[0]["selectedHistory"])
    assert max(len(r["selectedHistory"]) for r in requests[2:]) > len(
        requests[1]["selectedHistory"]
    )
    assert set(result.covered_ranges) == seen
    assert result.text == "完整交接，明细回读原始记录。"


def test_repeated_summary_truncation_is_bounded_and_keeps_original_history(monkeypatch):
    import pytest
    from compass_agent.models.errors import ModelError

    journal, models, control, budget = setup()
    original = long_tool_history()
    calls = []

    async def call(*args, request, **kwargs):
        calls.append(request)
        return reply(journal, request, text="不完整").model_copy(
            update={
                "status": "incomplete",
                "finish_reason": "length",
                "error_code": "output_truncated",
            }
        )

    monkeypatch.setattr("compass_agent.context_runtime.compaction.auxiliary_call", call)
    with pytest.raises(ModelError, match="output_truncated"):
        asyncio.run(
            summarize(
                journal,
                models,
                None,
                control,
                budget,
                SimpleNamespace(provider_id=journal.provider, binding_version=1),
                ModelTokenLimits(max_input_tokens=262144, max_output_tokens=2048),
                [original],
                "继续",
            )
        )
    assert len(calls) == 3
    assert len(original.messages) == 59

    from compass_agent.harness.context import ContextLimit

    optimizer, draft, frame = optimizer_frame(
        journal,
        [
            ChatMessage(role="system", content="固定规则" * 2500),
            ChatMessage(role="user", content="当前问题"),
        ],
    )
    optimizer.groups = [original]
    calls.clear()
    with pytest.raises(ContextLimit):
        asyncio.run(
            optimizer.optimize(
                frame,
                models,
                None,
                control,
                budget,
                SimpleNamespace(provider_id=journal.provider, binding_version=1),
                ModelTokenLimits(max_input_tokens=262144, max_output_tokens=2048),
                draft,
            )
        )
    assert len(calls) == 3
    assert optimizer.summary is None
    assert optimizer.groups == [original]


def test_confirmed_meaning_updates_fixed_context_across_incremental_steps_and_restore():
    from copy import deepcopy

    from compass_agent.context_runtime.optimizer import ContextOptimizer
    from compass_agent.schemas import GenerateInput

    journal, models, control, _ = setup()
    messages = [
        ChatMessage(role="system", content="固定系统"),
        ChatMessage(role="user", content='{"source":"publishedSemanticRegistry","items":[]}'),
        ChatMessage(role="user", content="当前问题"),
    ]
    optimizer, draft, frame = optimizer_frame(journal, messages)
    limits = ModelTokenLimits(max_input_tokens=100000, max_output_tokens=2048)
    confirmed = json.dumps(
        {
            "source": "publishedSemanticRegistry",
            "items": [],
            "owner_confirmed_meanings": [{"mention": "净收入", "definition": "按入账时间统计"}],
        },
        ensure_ascii=False,
    )

    async def scenario():
        first = await optimizer.optimize(frame, models, None, control, None, None, limits, draft)
        for instance in (optimizer, ContextOptimizer(journal)):
            instance.groups = []
            instance.restore(first["contextOptimization"], messages)
            following = deepcopy(first)
            following["input"]["request"]["messages"][1]["content"] = confirmed
            following["receipt"]["stepKey"] = "root/model:step-2"
            # The incoming original question stays unchanged; compilation updates fixed context.
            result = await instance.optimize(
                following,
                models,
                None,
                control,
                None,
                None,
                limits,
                GenerateInput(messages=messages, max_tokens=512),
            )
            assert result["input"]["request"]["messages"][1]["content"] == confirmed
            assert result["input"]["request"]["messages"][-1]["content"] == "当前问题"
            assert result["contextOptimization"]["active"] == first["contextOptimization"]["active"]

    asyncio.run(scenario())
