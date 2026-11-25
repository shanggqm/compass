"""Production optimizer/summary/anchor on PG, with explicit deterministic model responses."""

import asyncio
import json
import os
import re
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid4, uuid5

from compass_agent.context_runtime.compaction import request_size
from compass_agent.context_runtime.evidence import user_input
from compass_agent.context_runtime.history import TurnGroup, historical_messages
from compass_agent.context_runtime.optimizer import ContextOptimizer
from compass_agent.context_runtime.pipeline import fit, input_budget, pin
from compass_agent.goal.anchor import anchor
from compass_agent.goal.contracts import LedgerItem
from compass_agent.goal.ledger import Ledger
from compass_agent.runtime.context_results import ContextResults
from compass_agent.runtime.entities import AgentGoal
from compass_agent.schemas import (
    CallResponse,
    ChatMessage,
    DecisionAnswer,
    GenerateInput,
    ModelTokenLimits,
)
from compass_agent.tools.builtin.database_query import DatabaseResult, query_database_live

from apps.agent.tests.context_fixture import capability_grants
from apps.agent.tests.test_agent_run import CHAT, PROVIDER_ID
from apps.agent.tests.test_database_tools_integration import targets as targets
from apps.agent.tests.test_generic_database_integration import data_source as data_source
from apps.agent.tests.test_goal_runtime_integration import finish, source_runtime
from apps.agent.tests.test_tool_dispatcher import call, context

MARKERS = (
    "[OBJECTIVE monthly_review]",
    "[CONSTRAINT region=EAST]",
    "[FACT sales=350]",
    "[TODO inventory]",
    "[FAIL blocked_source]",
    "[SOURCE sales_receipt]",
)
PATTERN = re.compile(r"\[(?:OBJECTIVE|CONSTRAINT|FACT|TODO|FAIL|SOURCE) [^\]\n]{1,80}\]")
LIMITS = ModelTokenLimits(max_input_tokens=9000, max_output_tokens=1024)


class OfflineModels:
    """Same generic extractive summarizer for both optimized strategies."""

    def __init__(self):
        self.repository = SimpleNamespace(get=lambda _: SimpleNamespace(binding_version=1))
        self.calls = []

    async def prepare(self, call_id, provider_id, request, trusted, **kwargs):
        return SimpleNamespace(call_id=call_id, provider_id=provider_id)

    async def run(self, prepared, request, *, attempt_budget=None, **kwargs):
        wire = json.dumps(request.model_dump(mode="json", exclude_none=True), ensure_ascii=False)
        measured = len(wire.encode())
        self.calls.append({"kind": request.kind, "inputUtf8UpperBound": measured})
        counter = getattr(attempt_budget, "budget", attempt_budget)
        if counter:
            counter.tokens += measured
            counter.attempts += 1
        shared = dict(
            call_id=prepared.call_id,
            provider_id=prepared.provider_id,
            config_version=1,
            status="completed",
            duration_ms=0,
            requested_model="offline-extractive-fixture",
            kind=request.kind,
            finish_reason="stop",
        )
        if request.kind == "decision":
            return CallResponse.model_validate(
                {
                    **shared,
                    "answers": {
                        key: DecisionAnswer(
                            type="choice", choice="keep", probabilities={"keep": 1.0, "drop": 0.0}
                        )
                        for key in request.questions
                    },
                }
            )
        # Every output fact must occur in this actual request; no expected-answer lookup.
        text = "\n".join(dict.fromkeys(PATTERN.findall(wire)))
        return CallResponse.model_validate({**shared, "text": text or "该批材料无显式验收事实。"})


def groups_for(step):
    groups = []
    for index in range(8 + step):
        marker = " ".join(MARKERS) if index == 0 else f"阶段记录 {index}"
        messages = (
            ChatMessage(role="user", content=marker + "\n" + "经营明细待核对；" * 120),
            ChatMessage(role="assistant", content=f"已处理第 {index} 批明细。"),
        )
        identifier = str(uuid5(NAMESPACE_URL, f"compass-983-stage:{index}"))
        groups.append(
            TurnGroup(
                identifier,
                uuid5(NAMESPACE_URL, identifier),
                index + 1,
                messages,
                (identifier + "/user", identifier + "/answer"),
            )
        )
    return groups


def setup_goal(runtime):
    journal = runtime.journal
    identifier = user_input(journal, " ".join(MARKERS))
    with runtime.store.transaction() as tx:
        goal = tx.get(AgentGoal, runtime.goal_id, with_for_update=True)
        goal.contract = {**goal.contract, "objective": MARKERS[0], "constraints": [MARKERS[1]]}
        Ledger(runtime.store).record(
            tx,
            goal,
            [
                LedgerItem(
                    type="conclusion",
                    key="sales",
                    title=MARKERS[2],
                    text=MARKERS[5],
                    evidenceIds=[identifier],
                    acceptanceIds=["a1"],
                    factKey="sales",
                    value=Decimal("350"),
                    unit="分",
                ),
                LedgerItem(type="todo", key="inventory", title=MARKERS[3]),
                LedgerItem(type="failure", key="blocked-source", title=MARKERS[4]),
            ],
            journal,
            CHAT,
        )
        binding = {**pin(tx, CHAT, runtime.store), "answerProtocol": 2, "optimizationVersion": 1}
        _, run = runtime.store.lock(tx, journal.run_id, CHAT)
        run.config_snapshot = {**run.config_snapshot, "contextRuntime": binding}
    journal.context_binding = binding
    journal.context_results = ContextResults(journal)
    journal.capabilities = lambda: capability_grants(CHAT)


async def strategy(data_source, name):
    runtime = source_runtime(data_source, compass_routing_jev_provider_id=PROVIDER_ID)
    setup_goal(runtime)
    journal = runtime.journal
    models = OfflineModels()
    budget = SimpleNamespace(
        steps=0,
        tokens=0,
        cost_microusd=0,
        attempts=0,
        unknown_attempts=0,
        reason=None,
        profiles=[
            SimpleNamespace(
                provider_id=PROVIDER_ID, binding_version=1, input_tokens=200000, output_tokens=1024
            )
        ],
    )
    route = SimpleNamespace(provider_id=PROVIDER_ID, binding_version=1)
    ctx = context("database_query")
    queries = []

    async def read_sales():
        queries.append("sales")
        result = await query_database_live(
            call(
                "database_query",
                str(uuid4()),
                json.dumps(
                    {
                        "op": "query",
                        "sql": "SELECT sum(amount_cent)::text AS amount "
                        "FROM analytics_978.daily_fact WHERE channel='WEB'",
                    }
                ),
            ),
            data_source[2],
            ctx.control,
        )
        assert isinstance(result, DatabaseResult) and result.rows == (("350",),)
        return "[FACT sales=350]"

    await read_sales()
    observations = []
    for step in range(3):
        groups = groups_for(step)
        protected = anchor(journal, CHAT, None) if name == "goal_ledger" else ""
        system = "核对月度经营任务，依当前可见资料作答。" + ("\n" + protected if protected else "")
        current = ChatMessage(role="user", content=journal.user_question)
        draft = GenerateInput(
            messages=[ChatMessage(role="system", content=system), current],
            tools=[],
            stream=False,
            thinking=False,
            max_tokens=1024,
        )
        summary_ranges = []
        if name == "fixed_window":
            candidate = draft.model_copy(
                update={"messages": [draft.messages[0], *historical_messages(groups), current]}
            )
            value, before, after, _, cuts = fit(candidate, input_budget(LIMITS, 1024))
            compacted = bool(cuts)
            method = "whole_turn_truncation"
        else:
            optimizer = ContextOptimizer(journal)
            optimizer.groups = groups
            frame = {
                "input": {"request": draft.model_dump(mode="json")},
                "receipt": {"stepKey": f"{name}:{step}", "budget": {"protocolExtra": 0}},
            }
            frame = await optimizer.optimize(
                frame, models, CHAT, ctx.control, budget, route, LIMITS, draft
            )
            value = GenerateInput.model_validate(frame["input"]["request"])
            measured = frame["contextOptimization"]["measuredUsage"]
            before, after = measured["before"], measured["after"]
            compacted = frame["receipt"]["contextOptimization"]["compacted"]
            assert optimizer.summary is not None and compacted
            summary_ranges = list(optimizer.summary.covered_ranges)
            assert len(summary_ranges) == sum(len(g.messages) for g in groups)
            method = "stage_summary"
        visible = "\n".join(m.content or "" for m in value.messages)
        retained = [marker for marker in MARKERS if marker in visible]
        assert after <= input_budget(LIMITS, 1024)
        anchor_ok = all(marker in visible for marker in MARKERS) if protected else None
        if protected:
            assert anchor_ok
        if "[FACT sales=350]" not in visible:
            visible += "\n" + await read_sales()
        delivered = [marker for marker in MARKERS if marker in visible]
        observations.append(
            {
                "step": step + 1,
                "method": method,
                "compacted": compacted,
                "beforeUtf8UpperBound": before,
                "afterUtf8UpperBound": after,
                "mainRequestUtf8UpperBound": request_size(value),
                "factsRetained": retained,
                "factRetention": len(retained) / len(MARKERS),
                "protectedAnchorIntact": anchor_ok,
                "coveredSummaryRanges": summary_ranges,
                "completed": len(delivered) == len(MARKERS),
            }
        )
    finish(
        runtime, status="failed"
    )  # The controlled harness does not submit a generated final_answer.
    return {
        "strategy": name,
        "completionRate": float(observations[-1]["completed"]),
        "keyFactRetention": sum(o["factRetention"] for o in observations) / len(observations),
        "physicalQueryCount": len(queries),
        "repeatQueryCount": len(queries) - 1,
        "tokenUsage": {
            "method": "utf8_conservative_upper_bound",
            "providerReported": None,
            "mainInputs": sum(o["mainRequestUtf8UpperBound"] for o in observations),
            "auxiliaryInputs": sum(c["inputUtf8UpperBound"] for c in models.calls),
        },
        "auxiliaryCalls": models.calls,
        "steps": observations,
    }


def test_three_strategies_produce_measured_report(data_source):
    rows = [
        asyncio.run(strategy(data_source, name))
        for name in ("fixed_window", "summary_only", "goal_ledger")
    ]
    assert all(row["physicalQueryCount"] >= 1 for row in rows)
    assert all(step["compacted"] for row in rows for step in row["steps"])
    assert rows[2]["keyFactRetention"] == 1.0 and rows[2]["completionRate"] == 1.0
    report = {
        "feature": 983,
        "mode": "offline_deterministic_model_with_real_postgresql",
        "dataset": "same_monthly_review_fixture_v1",
        "inputBudget": LIMITS.max_input_tokens,
        "realModelQualityEvaluation": False,
        "strategies": rows,
    }
    output = Path(os.environ.get("COMPASS_983_STRATEGY_REPORT", "work/983-context-strategies.json"))
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
