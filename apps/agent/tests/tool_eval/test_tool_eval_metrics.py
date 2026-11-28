"""Deterministic metric/diagnostic tests; no provider, database or service required."""

import asyncio
import json
from dataclasses import replace
from uuid import UUID

import pytest
from compass_agent.schemas import ToolCall, ToolCallFunction

from . import runner
from .harness import ModelTurn, declarations, load_dataset, offline_case, run_case
from .metrics import CaseEvalRecord, RecoveryTracker, recompute_report, summarize_group
from .runner import execute, parser
from .sql_fixture import query as synthetic_sql


def row(**changes):
    default = CaseEvalRecord(
        "case-1",
        True,
        frozenset({"query_metric"}),
        frozenset({"schedule_write"}),
        ("query_metric",),
        1,
        1,
        True,
        0,
        0,
        0,
        10,
        100,
        0,
        30,
        12,
    )
    return replace(default, **changes)


def case(case_id):
    return next(c for c in load_dataset()["cases"] if c["id"] == case_id)


def turn(name, args):
    return ModelTurn(
        "completed",
        "",
        [ToolCall(id="call-1", function=ToolCallFunction(name=name, arguments=json.dumps(args)))],
        input_tokens=10,
        output_tokens=2,
    )


def scripted(*turns):
    remaining = list(turns)

    async def model(_messages, _definitions):
        assert remaining, "unexpected additional model call"
        return remaining.pop(0)

    return model


def run(*args, **kwargs):
    return asyncio.run(run_case(*args, **kwargs))


def answer(text="本次合成数据华东 GMV 为 20000 分。"):
    return ModelTurn("completed", text, [], input_tokens=10, output_tokens=2)


def test_six_primary_and_four_guardrails_use_weighted_denominators():
    a = row()
    b = row(
        case_id="case-2",
        completed=False,
        called_tools=("query_metric", "query_metric", "schedule_write"),
        total_calls=3,
        valid_argument_calls=1,
        first_turn_succeeded=False,
        redundant_calls=1,
        recoverable_errors=2,
        recovered_errors=1,
        declaration_tokens_sum=90,
        prompt_tokens_sum=900,
        clarifications=2,
        elapsed_ms=70,
        cost_microusd=28,
    )
    result = summarize_group("C", [a, b])
    assert result["primary"] == {
        "tool_selection_accuracy": 0.5,
        "argument_validity_rate": 0.5,
        "first_turn_success_rate": 0.5,
        "redundant_call_rate": 0.25,
        "error_recovery_rate": 0.5,
        "tool_declaration_token_share": 0.1,
    }
    assert result["guardrails"] == {
        "task_completion_rate": 0.5,
        "avg_clarifications": 1,
        "avg_elapsed_ms": 50,
        "avg_cost_microusd": 20,
    }


def test_zero_denominator_is_not_applicable_and_empty_sample_is_rejected():
    empty = row(
        called_tools=(),
        total_calls=0,
        valid_argument_calls=0,
        declaration_tokens_sum=0,
        prompt_tokens_sum=0,
    )
    metrics = summarize_group("A", [empty])["primary"]
    assert metrics["argument_validity_rate"] is None
    assert metrics["redundant_call_rate"] is None
    assert metrics["error_recovery_rate"] is None
    assert metrics["tool_declaration_token_share"] is None
    assert metrics["tool_selection_accuracy"] == 0
    with pytest.raises(ValueError, match="at least one"):
        summarize_group("A", [])


def test_missing_cost_or_tokens_keeps_partial_aggregate_unknown():
    result = summarize_group(
        "B",
        [
            row(),
            row(
                case_id="case-2",
                cost_microusd=None,
                declaration_tokens_sum=None,
                prompt_tokens_sum=None,
            ),
        ],
    )
    assert result["guardrails"]["avg_cost_microusd"] is None
    assert result["primary"]["tool_declaration_token_share"] is None
    assert result["denominators"]["cost_coverage_cases"] == 1
    assert result["denominators"]["token_coverage_cases"] == 1
    assert summarize_group("B", [row(cost_microusd=0)])["guardrails"]["avg_cost_microusd"] == 0


@pytest.mark.parametrize(
    "changes",
    [
        {"total_calls": -1},
        {"valid_argument_calls": 2},
        {"redundant_calls": 2},
        {"recoverable_errors": 0, "recovered_errors": 1},
        {"cost_microusd": -1},
        {"prompt_tokens_sum": 9},
        {"prompt_tokens_sum": None},
        {"total_calls": 2},
    ],
)
def test_invalid_metric_records_are_rejected(changes):
    with pytest.raises(ValueError):
        row(**changes)


def test_duplicate_case_ids_cannot_silently_change_sample_weights():
    with pytest.raises(ValueError, match="duplicate"):
        summarize_group("C", [row(), row()])


def test_empty_expected_tool_set_requires_successful_clarification_or_refusal():
    assert (
        summarize_group("C", [row(expected_primary_tools=frozenset(), completed=False)])["primary"][
            "tool_selection_accuracy"
        ]
        == 0
    )


@pytest.mark.parametrize("repair_round,recovered", [(1, 0), (2, 1), (3, 1), (4, 0)])
def test_recovery_window_excludes_parallel_siblings_and_late_success(repair_round, recovered):
    tracker = RecoveryTracker()
    tracker.failed("database_query", 1)
    tracker.succeeded("database_query", repair_round)
    assert tracker.recovered == recovered


def test_recovery_does_not_double_count_or_cross_tools():
    tracker = RecoveryTracker()
    tracker.failed("query_metric", 1)
    tracker.succeeded("resolve_semantics", 2)
    assert tracker.recovered == 0
    tracker.succeeded("query_metric", 3)
    tracker.succeeded("query_metric", 3)
    assert tracker.recovered == 1


def test_dataset_has_all_28_cases_in_eight_dimensions_and_original_baseline():
    data = load_dataset()
    assert sorted(data["dimension_counts"].values()) == [3, 3, 3, 3, 3, 4, 4, 5]
    old, _ = declarations("A", case("near-02"))
    new, _ = declarations("B", case("near-02"))
    assert len(old) == len(new) == 19
    assert any("$defs" in d.function.parameters for d in old)
    assert not any("$defs" in d.function.parameters for d in new)


def test_actual_three_channel_assembly_and_49_tool_capacity():
    ordinary = offline_case("C", case("pool-01"))
    assert ordinary["status"] == "assembled"
    assert ordinary["trace"]["authorized_count"] == 49
    assert len(ordinary["trace"]["group_visible"]) == 11
    for group in ("A", "B"):
        assert offline_case(group, case("pool-01"))["status"] == "tool_catalog_limit"
    selected = offline_case("C", case("pool-02"))["trace"]["group_visible"]
    assert len(selected) == 13
    assert "mcp_stock_00" in selected
    assert "mcp_stock_01" not in selected
    crowded = offline_case("C", case("pool-03"))["trace"]["group_visible"]
    assert len(crowded) == 24
    assert "python_analysis" in crowded


def test_permission_filtering_and_public_selection_validation_are_real():
    extension = offline_case("C", case("access-01"))
    assert "mcp_stock_00" not in str(extension["trace"])
    semantic = offline_case("C", case("access-02"))
    assert "submit_semantic_change" not in str(semantic["trace"])
    assert offline_case("C", case("access-03"))["status"] == "selection_rejected"


def test_scripted_repair_calls_real_readonly_sql_validation_and_v3_answer_turn():
    model = scripted(
        turn("database_query", {"sql": "SELECT SUM(amount_cents) FROM sales.orders"}),
        turn("database_query", {"sql": "SELECT SUM(amount_cent) FROM sales.orders"}),
        turn("final_answer", {"mode": "evidence"}),
        answer(),
    )
    record, trace = run("C", case("repair-03"), model)
    assert record.completed
    assert record.recoverable_errors == record.recovered_errors == 1
    assert not record.first_turn_succeeded
    assert record.valid_argument_calls == 3
    assert trace["rounds"][0]["receipts"][0]["payload"]["field"] == "sql"
    assert record.cost_microusd is None


def test_argument_error_and_cost_from_actual_usage():
    target = case("pre-01")
    ref = load_dataset()["synthetic_data"]["query_ref"]
    record, trace = run(
        "B",
        target,
        scripted(
            turn("query_metric", {}),
            turn("query_metric", {"query_ref": ref}),
            turn("final_answer", {"mode": "evidence"}),
            answer(),
        ),
        input_usd_per_million=1,
        output_usd_per_million=2,
    )
    assert record.completed
    assert record.valid_argument_calls == 2 and record.total_calls == 3
    assert record.recovered_errors == 1
    assert record.cost_microusd == 56
    assert trace["rounds"][0]["receipts"][0]["payload"]["field"] == "query_ref"


def test_successful_duplicate_counted_once_and_repair_not_redundant():
    ref = load_dataset()["synthetic_data"]["query_ref"]
    record, _ = run(
        "C",
        case("pre-01"),
        scripted(
            turn("query_metric", {"query_ref": ref}),
            turn("query_metric", {"query_ref": ref}),
            turn("final_answer", {"mode": "evidence"}),
            answer(),
        ),
    )
    assert record.completed and record.redundant_calls == 1


def test_wrong_argument_goal_does_not_pass_from_tool_name_alone():
    record, trace = run(
        "C",
        case("near-04"),
        scripted(
            turn("read_document", {"ref": "doc-policy-1", "offset": 1}),
            turn("final_answer", {"mode": "evidence"}),
            answer(),
        ),
    )
    assert not record.completed
    assert not trace["rubric"]["required_calls_matched"]


def test_ambiguity_receipt_is_a_successful_clarification_step():
    record, _ = run(
        "C",
        case("clarify-01"),
        scripted(
            turn("resolve_semantics", {"mention": "净销售额"}),
            turn("final_answer", {"mode": "plain"}),
            answer("请确认您需要含税还是不含税的净销售额？"),
        ),
    )
    assert record.completed and record.clarifications == 1
    assert record.recoverable_errors == 0


def test_direct_text_cannot_skip_required_final_answer_after_tool_work():
    record, trace = run(
        "C",
        case("near-02"),
        scripted(
            turn("query_metric", {"query_ref": load_dataset()["synthetic_data"]["query_ref"]}),
            answer(),
        ),
    )
    assert not record.completed
    assert trace["status"] == "missing_final_answer"


def test_unexpected_clarification_is_included_in_guardrail():
    record, _ = run(
        "C",
        case("near-02"),
        scripted(
            turn("query_metric", {"query_ref": load_dataset()["synthetic_data"]["query_ref"]}),
            turn("final_answer", {"mode": "plain"}),
            answer("请问还需要什么？"),
        ),
    )
    assert record.clarifications == 1
    assert not record.completed


def test_invisible_tool_is_not_a_recoverable_parameter_error():
    record, trace = run(
        "C",
        case("access-01"),
        scripted(
            turn("mcp_stock_00", {"sku": "SKU-101"}),
            turn("final_answer", {"mode": "plain"}),
            answer("需要取得权限。"),
        ),
    )
    assert not record.completed and record.recoverable_errors == 0
    assert trace["rounds"][0]["receipts"][0]["payload"]["category"] == "permission_denied"


def test_model_failure_keeps_completion_and_unknown_usage_honest():
    record, trace = run(
        "B",
        case("near-02"),
        scripted(ModelTurn("failed", "", [])),
        input_usd_per_million=1,
        output_usd_per_million=2,
    )
    assert not record.completed
    assert record.cost_microusd is None
    assert trace["status"] == "model_failed"


def test_offline_report_never_claims_model_metrics(tmp_path):
    args = parser().parse_args(["--mode", "offline", "--out", str(tmp_path / "report.json")])
    report, path = asyncio.run(execute(args))
    assert len(report["runs"]) == 84
    assert report["summaries"] == []
    assert "model" not in report
    assert path.exists()
    with pytest.raises(ValueError, match="output exists"):
        asyncio.run(execute(args))


def test_model_ablation_respects_concurrency_and_summarizes_all_groups(tmp_path, monkeypatch):
    active, peak = 0, 0

    async def model(_messages, _definitions):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return answer("请确认要查询哪个期间？")

    monkeypatch.setattr(runner, "_model_port", lambda _: (model, {"api_model": "scripted"}))
    args = parser().parse_args(
        [
            "--mode",
            "model",
            "--case",
            "clarify-02",
            "--concurrency",
            "2",
            "--out",
            str(tmp_path / "parallel.json"),
        ]
    )
    report, _ = asyncio.run(execute(args))
    assert peak == 2 and active == 0
    assert len(report["runs"]) == len(report["summaries"]) == 3
    assert all(
        summary["guardrails"]["task_completion_rate"] == 1 for summary in report["summaries"]
    )


def test_fixture_identifiers_remain_valid_in_both_old_and_new_schemas():
    data = load_dataset()
    for name in ("metric_id", "document_ref", "query_ref"):
        assert str(UUID(data["synthetic_data"][name])) == data["synthetic_data"][name]


def test_sql_fixture_returns_actual_selected_columns_grouping_and_values():
    result = synthetic_sql(
        "SELECT status, COUNT(*) AS n, SUM(amount_cent) AS total_cent FROM sales.orders "
        "WHERE business_date = DATE '2026-10-30' GROUP BY status"
    )
    assert result["columns"] == ["status", "n", "total_cent"]
    assert result["rows"] == [["completed", 2, 20000]]
    assert synthetic_sql(
        "SELECT order_id, amount_cent FROM sales.orders WHERE amount_cent < 0 LIMIT 20"
    )["rows"] == [["O-101", -500]]


def test_clarification_without_question_mark_is_recognized():
    record, _ = run("C", case("clarify-02"), scripted(answer("请告诉我您要查询的时间范围。")))
    assert record.completed and record.clarifications == 1


def test_saved_case_records_recompute_without_external_services():
    source = row()
    report = {
        "mode": "model",
        "status": "completed",
        "runs": [{"group": "C", "repeat": 1, "record": json.loads(json.dumps(source.to_dict()))}],
    }
    assert recompute_report(report) == [{"repeat": 1, **summarize_group("C", [source])}]
    with pytest.raises(ValueError, match="completed model report"):
        recompute_report({**report, "status": "running"})
