"""Concurrent children share the Run's hard token/cost limits and release reservations."""

import asyncio
from dataclasses import replace
from uuid import uuid4

import pytest
from compass_agent.harness.autonomy import AutonomyBudget, AutonomyLimits
from compass_agent.llm.contracts import GenerationOutput, RuntimeResult
from compass_agent.schemas import ModelUsage, OutputValidation

from .test_run_autonomy import profile
from .test_runtime import request


def settle(budget, attempt, req, *, tokens=100, output=10, unknown=False):
    budget.settle(
        attempt,
        None
        if unknown
        else RuntimeResult(
            call_id=req.context.call_id,
            provider_id=req.access.spec.provider_id,
            config_version=1,
            binding_version=1,
            requested_model=req.access.spec.api_model,
            status="completed",
            duration_ms=1,
            output=GenerationOutput(),
            usage=ModelUsage(input_tokens=tokens, output_tokens=output),
            validation=OutputValidation(),
            upstream_sent=True,
        ),
    )


def test_two_large_context_children_fit_shared_budget_and_parent_can_continue():
    async def scenario():
        req = request(max_tokens=8192)
        model = profile(
            req,
            input_tokens=1_050_000,
            output_tokens=32768,
            input_microusd=4,
            output_microusd=15,
        )
        parent = AutonomyBudget((model,), AutonomyLimits())
        parent.tokens, parent.cost_microusd = 145315, 591923
        before = (parent.tokens, parent.cost_microusd)
        with parent.child_budget(steps=8) as first, parent.child_budget(steps=8) as second:
            a, b = uuid4(), uuid4()
            assert first.reserve(req, a)
            assert second.reserve(req, b)
            assert parent.committed("cost_microusd") == 591923 + 2 * 4322880
            assert parent.committed("tokens") == 145315 + 2 * (1050000 + 8192)
            settle(first, a, req)
            # Released capacity is available to the next child call immediately.
            a = uuid4()
            assert first.reserve(req, a)
            settle(first, a, req)
            settle(second, b, req)
        assert parent.tokens == before[0] + 330
        assert parent.cost_microusd == before[1] + 1650
        assert parent.attempts == 3
        assert not parent.children
        assert parent.reserve(req, uuid4())

    asyncio.run(scenario())


@pytest.mark.parametrize("resource", ["tokens", "cost_microusd"])
def test_concurrent_reservations_cannot_exceed_shared_limit(resource):
    async def scenario():
        req = request(max_tokens=10)
        model = profile(req, input_tokens=100, input_microusd=1, output_microusd=1)
        limits = replace(AutonomyLimits(), **{resource: 200})
        parent = AutonomyBudget((model,), limits)
        with parent.child_budget() as first, parent.child_budget() as second:
            a = uuid4()
            assert first.reserve(req, a)
            assert not second.reserve(req, uuid4())
            assert second.reason == ("token_budget" if resource == "tokens" else "cost_budget")
            assert parent.committed(resource) == 110
            settle(first, a, req, tokens=50, output=5)
        assert parent.reason is None
        assert parent.committed(resource) == 55
        assert parent.reserve(req, uuid4())

    asyncio.run(scenario())


def test_unknown_child_usage_keeps_full_reservation_charged_after_exit():
    async def scenario():
        req = request(max_tokens=10)
        model = profile(req, input_tokens=100, input_microusd=1, output_microusd=1)
        parent = AutonomyBudget((model,), AutonomyLimits(tokens=200, cost_microusd=200))
        with parent.child_budget() as child:
            assert child.reserve(req, uuid4())
            # Simulate interruption before settlement: child teardown settles as unknown.
        assert parent.tokens == parent.cost_microusd == 110
        assert parent.unknown_attempts == 1
        assert parent.committed("tokens") == 110
        assert not parent.reserve(req, uuid4())
        assert parent.reason == "token_budget"

    asyncio.run(scenario())


def test_parent_reservation_is_visible_to_children():
    async def scenario():
        req = request(max_tokens=10)
        model = profile(req, input_tokens=100, input_microusd=1, output_microusd=1)
        parent = AutonomyBudget((model,), AutonomyLimits(tokens=200, cost_microusd=200))
        assert parent.reserve(req, uuid4())
        with parent.child_budget() as child:
            assert not child.reserve(req, uuid4())
        assert parent.committed("tokens") == 110

    asyncio.run(scenario())
