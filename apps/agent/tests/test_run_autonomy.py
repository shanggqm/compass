"""Generic tool-loop limits and reliable execution acceptance."""

import asyncio
import json
from dataclasses import replace
from typing import Any, cast
from uuid import uuid4

import pytest
from compass_agent.harness.autonomy import AutonomyBudget, AutonomyLimits, AutonomyStopped
from compass_agent.harness.verification import VerificationRejected
from compass_agent.llm.contracts import GenerationOutput, RuntimeResult
from compass_agent.llm.errors import RuntimeFailure
from compass_agent.llm.policy import LogicalPolicy
from compass_agent.runtime.context import compass_definition
from compass_agent.runtime.control import OwnerKey, RunRegistry
from compass_agent.runtime.service import RunService
from compass_agent.schemas import (
    DEFAULT_RUN_TIMEOUT_SECONDS,
    ModelUsage,
    OutputValidation,
    RunConstraints,
    StreamExtension,
    ToolCall,
    ToolCallFunction,
)
from compass_agent.settings import AutonomyModelProfile
from compass_agent.tools.builtin.database_query import DatabaseResult
from compass_agent.tools.builtin.database_query.handler import registration
from compass_agent.tools.contracts import PhaseCanceled, StopRequested

from .test_agent_run import (
    CHAT,
    FakeModels,
    FakeRouting,
    binding,
    model_result,
    run_command,
    selected_route,
)
from .test_reliable_executor import executor
from .test_runtime import request


def profile(req, **changes):
    spec = req.access.spec
    return AutonomyModelProfile(
        provider_id=spec.provider_id,
        binding_version=spec.binding_version,
        sdk_model=spec.sdk_model,
        api_base=spec.api_base,
        input_tokens=100,
        output_tokens=2048,
        input_microusd=2,
        output_microusd=8,
    ).model_copy(update=changes)


def tool(name, call_id, **arguments):
    return ToolCall(
        id=call_id,
        function=ToolCallFunction(name=name, arguments=json.dumps(arguments)),
    )


def output(*calls):
    return model_result(tool_calls=calls, token="fixture-token", finish="tool_calls")


class ScriptedModels(FakeModels):
    measured_usage: ModelUsage | None = None

    async def run(self, prepared, value, **kwargs):
        action = self.results.pop(0)
        ledger = kwargs["attempt_budget"]
        req = replace(request(), input=value)
        ledger.profiles = (profile(req),)
        req = replace(req, context=replace(req.context, call_id=prepared.call_id))
        assert ledger.reserve(req, prepared.call_id)
        receipt = None
        if self.measured_usage is not None:
            receipt = RuntimeResult(
                call_id=prepared.call_id,
                provider_id=req.access.spec.provider_id,
                config_version=1,
                binding_version=req.access.spec.binding_version,
                requested_model=req.access.spec.api_model,
                status="completed",
                duration_ms=1,
                output=GenerationOutput(),
                usage=self.measured_usage,
                validation=OutputValidation(),
                upstream_sent=True,
            )
        ledger.settle(prepared.call_id, receipt)
        return action(value) if callable(action) else action


@pytest.mark.parametrize(
    ("limits", "reason"),
    [
        (AutonomyLimits(tokens=150), "token_budget"),
        (AutonomyLimits(cost_microusd=450), "cost_budget"),
    ],
)
def test_reliable_executor_rechecks_budget_after_unknown_usage(limits, reason):
    async def scenario():
        req = request(5, max_tokens=10)
        budget = AutonomyBudget((profile(req),), limits)
        budget.generation = req.context.call_id
        sent = 0

        async def action(_req, session):
            nonlocal sent
            sent += 1
            session.resources.sent = True
            session.progress.usage = ModelUsage()
            raise RuntimeFailure("model_rate_limited", recovery="rejected_transient")

        result = await executor(action, jitter=lambda *_: 0).execute(
            req, LogicalPolicy(max_attempts=2), attempt_budget=budget
        )
        assert result.status == "failed" and sent == 1
        assert budget.reason == reason
        summary = budget.snapshot()
        assert summary.steps.used == 1 and summary.attempts == 1
        assert summary.tokens.used == 110 and summary.unknown_attempts == 1
        assert result.execution and len(result.execution.attempts) == 1

    asyncio.run(scenario())


def test_user_stop_during_short_phase_cleanup_preserves_receipt():
    async def scenario():
        control = RunRegistry().register(OwnerKey.from_context(CHAT))
        cleaning, release = asyncio.Event(), asyncio.Event()

        async def phase():
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cleaning.set()
                await release.wait()
                raise PhaseCanceled(cleanup_confirmed=True) from None

        task = asyncio.create_task(control.perform(phase, timeout_s=0.03))
        await asyncio.wait_for(cleaning.wait(), timeout=2)
        assert control.request_stop("user").status == "accepted"
        release.set()
        with pytest.raises(StopRequested):
            await task
        assert control.cleanup_confirmed and control.stop_reason == "user"

    asyncio.run(scenario())


PERIOD = {"start": "2025-11-17", "end": "2025-11-24"}
COMPARE = {"start": "2025-11-24", "end": "2025-12-01"}


def channel():
    return tool("database_query", "c", op="query", sql="SELECT 1 AS amount")


def product():
    return tool("database_query", "p", op="query", sql="SELECT 2 AS amount")


def definition():
    return tool(
        "todo_write",
        "plan",
        op="replace",
        baseVersion=0,
        items=[{"id": "t1", "goal": "查询并计算"}],
        selectTodoId="t1",
    )


def select(todo="t1", version=1):
    return tool("todo_write", "select", op="select", baseVersion=version, todoId=todo)


def complete(todo="t1", source=None, version=1):
    return tool("todo_write", "finish", op="complete", baseVersion=version, todoId=todo)


def source(input_, call_id):
    return next(
        json.loads(m.content)["resultId"]
        for m in input_.messages
        if m.role == "tool" and m.tool_call_id == call_id and m.content
    )


async def execute(
    actions,
    *,
    limits=None,
    constraints=None,
    query_delay=0,
    default_verifiers=(),
    task_verifiers=None,
    verifiers=(),
    autonomy=True,
    question=None,
    **unused,
):
    models = ScriptedModels(actions) if autonomy else FakeModels(actions)
    events, queried = [], []
    control = RunRegistry().register(
        OwnerKey.from_context(CHAT),
        lifetime_s=(constraints.timeout_seconds if constraints else None)
        or DEFAULT_RUN_TIMEOUT_SECONDS,
    )

    async def query(call, bound, phase):
        queried.append(call.id)
        try:
            if query_delay:
                await asyncio.sleep(query_delay)
        except asyncio.CancelledError:
            raise PhaseCanceled(cleanup_confirmed=True) from None
        return DatabaseResult(str(uuid4()), ("amount",), ((3,),), False)

    async def provider(trusted, control, emit):
        return (registration((binding(),), query=query, emit=emit),)

    runner = RunService(
        cast(Any, models),
        cast(Any, FakeRouting(selected_route())),
        definition=compass_definition(),
        tools_provider=provider,
        autonomy_profiles=() if autonomy else None,
        autonomy_limits=limits,
        verifiers=default_verifiers,
        task_verifiers=task_verifiers,
    )
    command = run_command().model_copy(
        update={"constraints": constraints, "question": question or "查询数据并汇总"}
    )
    result = await runner.run(command, CHAT, control, events.append, verifiers=verifiers)
    extensions: list[dict[str, Any]] = [
        cast(dict[str, Any], e.payload.data)
        for e in events
        if isinstance(e, StreamExtension) and e.payload.name.startswith("compass.autonomy.")
    ]
    return result, queried, extensions


@pytest.mark.parametrize("steps", [1, 2, 3])
def test_round_limits_stop_further_tool_dispatch(steps):
    result, queried, events = asyncio.run(
        execute([output(channel())] * steps, constraints=RunConstraints(maxSteps=steps))
    )
    assert result.status == "failed" and result.error_code == "step_limit"
    assert len(queried) == steps - 1


def test_todo_and_queries_use_the_same_generic_loop():
    result, queried, events = asyncio.run(
        execute(
            [
                output(definition()),
                output(channel()),
                output(complete()),
                model_result(text="汇总完成。"),
            ]
        )
    )
    assert result.status == "completed" and result.final_answer == "汇总完成。"
    assert queried == ["c"]
    assert any(
        event.get("items", [{}])[0].get("goal") == "查询并计算"
        for event in events
        if event.get("items")
    )


def test_timeout_cancels_query_and_confirms_cleanup():
    result, queried, events = asyncio.run(
        execute([output(channel())], constraints=RunConstraints(timeoutSeconds=1), query_delay=20)
    )
    assert result.status == "failed" and result.error_code == "run_timeout"
    assert queried == ["c"]


@pytest.mark.parametrize("reject", [False, True])
def test_result_hooks_still_gate_publication(reject):
    seen = []

    async def check(candidate, context):
        seen.append(candidate.final_answer)
        if reject:
            raise VerificationRejected("rejected")

    result, _, _ = asyncio.run(execute([model_result(text="完成")], default_verifiers=(check,)))
    assert seen == ["完成"]
    assert result.status == ("failed" if reject else "completed")


def test_tool_budget_stops_before_extra_dispatch():
    result, queried, _ = asyncio.run(
        execute([output(channel()), output(product())], limits=AutonomyLimits(tools=1))
    )
    assert result.status == "failed" and queried == ["c"]


def test_paused_goal_stops_run_as_user_cancellation():
    def paused(_input):
        raise AutonomyStopped("goal_paused")

    result, queried, _ = asyncio.run(execute([paused]))
    assert result.status == "canceled" and result.reason == "user"
    assert result.cleanup == "confirmed" and queried == []
