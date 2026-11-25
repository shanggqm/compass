"""Unit regressions for fixed plans, row authority and the receipt content boundary."""

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from compass_agent.schemas import SemanticResultDecision
from compass_agent.semantic.execution import choose_connection, json_value
from compass_agent.semantic.service import RegistryTools, open_digest
from compass_agent.tools.builtin.database_query import DatabaseResult
from compass_agent.tools.business.query_metric import Input
from compass_agent.tools.contracts import ToolRejected


def test_query_ref_is_sufficient_but_changed_parameters_cannot_reuse_it():
    ref = uuid4()
    assert Input(query_ref=ref).parameters is None
    metric = {"source_ref": "absent", "access_requirements": {}}
    with pytest.raises(ToolRejected):
        choose_connection(
            [SimpleNamespace(connection_id=uuid4(), name="other", scope_check=None)], metric
        )


def test_persisted_result_hash_tracks_values_columns_and_truncation():
    data = DatabaseResult("opaque", ("amount",), ((100,),), False)
    assert open_digest(data) == open_digest(DatabaseResult("replay", ("amount",), ((100,),), False))
    assert open_digest(data) != open_digest(DatabaseResult("opaque", ("amount",), ((99,),), False))
    assert open_digest(data) != open_digest(DatabaseResult("opaque", ("amount",), ((100,),), True))
    assert json_value(Decimal("10.200")) == "10.200"


def test_temporal_or_group_changes_require_a_new_resolution(monkeypatch):
    import asyncio

    from compass_agent.semantic import service

    obj = object.__new__(RegistryTools)
    obj.journal = SimpleNamespace(search_snapshot=None)
    params: dict[str, Any] = {
        "start": "2026-03-01",
        "end": "2026-04-01",
        "as_of": "2026-05-01T00:00:00+00:00",
    }
    plan = {"asset_id": "a", "version_id": "v", "binding_id": "b", "parameters": params}
    monkeypatch.setattr(obj, "get", lambda *_: plan)
    obj.api = AsyncMock(return_value={"binding": {}})
    obj.store = SimpleNamespace(engine=None, source_settings=None)
    obj.trusted = SimpleNamespace(scope=None)
    monkeypatch.setattr(
        service, "DataConnections", lambda *_: SimpleNamespace(bindings_for_run=lambda _: [])
    )
    monkeypatch.setattr(service, "choose_connection", lambda *_: None)
    for change in (
        {"as_of": "2026-05-02T00:00:00Z"},
        {"group_by": "region"},
        {"end": "2026-04-02"},
    ):
        with pytest.raises(ToolRejected, match="semantic_rebind_parameters"):
            asyncio.run(obj.query({"query_ref": "r", "parameters": {**params, **change}}, None))

    params["group_by"] = "district"
    with pytest.raises(ToolRejected, match="invalid_parameters"):
        asyncio.run(obj.query({"query_ref": "r", "operation": "drilldown"}, None))
    params["filters"] = {"country": "CN"}
    with pytest.raises(ToolRejected, match="semantic_rebind_parameters"):
        asyncio.run(
            obj.query(
                {"query_ref": "r", "operation": "drilldown", "parent_group": {"country": "JP"}},
                None,
            )
        )

    calculate = AsyncMock(side_effect=[([], {"region_ids": [1, 2]}), ([], {"region_ids": [1]})])
    monkeypatch.setattr(service, "calculate", calculate)
    previous = {**params, "start": "2026-02-01", "end": "2026-03-01"}
    with pytest.raises(ToolRejected, match="source_access_denied"):
        asyncio.run(
            obj.query(
                {"query_ref": "r", "operation": "compare", "comparison": previous},
                SimpleNamespace(control=None),
            )
        )


def test_search_cursor_is_bound_to_its_release_and_original_conditions(monkeypatch):
    import asyncio
    from contextlib import nullcontext

    from compass_agent.semantic.entities import SemanticMetricOffer

    obj = object.__new__(RegistryTools)
    obj.journal = SimpleNamespace(run_id=uuid4(), epoch=uuid4())
    obj.trusted = SimpleNamespace()
    obj.pins = [str(uuid4())]
    saved = {}
    offsets = []
    visible_id = "one"

    async def api(op, args=None):
        assert op == "search" and args is not None
        offsets.append(args["cursor"])
        return {"items": [{"asset_id": visible_id}], "cursor": 5 if args["cursor"] == 0 else None}

    tx = SimpleNamespace(
        get=lambda model, ref: saved.get(ref),
        add=lambda row: saved.update({row.offer_id: row}),
        scalar=lambda _: sum(row.kind == "search" for row in saved.values()),
    )
    obj.store = SimpleNamespace(
        lock=lambda *_: (SimpleNamespace(id=uuid4(), owner_user_id=uuid4()), None),
        transaction=lambda: nullcontext(tx),
        require_epoch=lambda *_: None,
        private=SimpleNamespace(
            seal=lambda _, key, payload: payload, open=lambda _, key, payload: payload
        ),
    )
    monkeypatch.setattr(obj, "api", api)
    monkeypatch.setattr(obj, "record_page", lambda *_: None)
    monkeypatch.setattr(
        obj, "get", lambda ref, kind: tx.get(SemanticMetricOffer, UUID(ref)).private_payload
    )

    def call():
        return SimpleNamespace(step_id=uuid4(), call_id=str(uuid4()))

    query = {"query": "件数", "limit": 5}
    original = call()
    page = asyncio.run(obj.run("search", query, original))
    assert page["cursor"] and page["cursor"] != 5
    assert asyncio.run(obj.run("search", query, original)) == page
    assert asyncio.run(obj.run("search", query, call()))["status"] == "budget_exhausted"
    more = {**query, "cursor": page["cursor"]}
    assert asyncio.run(obj.run("search", more, call()))["cursor"] is None
    with pytest.raises(ToolRejected, match="invalid_parameters"):
        asyncio.run(obj.run("search", {**more, "query": "退货"}, call()))
    visible_id = "changed"
    with pytest.raises(ToolRejected, match="source_access_denied"):
        asyncio.run(obj.run("search", query, original))
    obj.pins = [str(uuid4())]
    with pytest.raises(ToolRejected, match="invalid_parameters"):
        asyncio.run(obj.run("search", more, call()))
    assert offsets == [0, 0, 0, 5, 0]


def test_period_difference_preserves_sign_precision_and_missing_groups():
    from compass_agent.semantic.execution import period_comparison

    def row(group, value):
        return {
            "group_key": group,
            "value": value,
            "status": "OK" if value is not None else "NO_DATA",
        }

    current = [row("east", "103.10"), row("north", 0), row("south", 10), row("west", None)]
    previous = [row("north", 7), row("east", "100.05"), row("west", 10)]
    compared = period_comparison(current, previous)
    assert [r["current_minus_comparison"] for r in compared] == [
        "3.05",
        "-7",
        None,
        None,
        None,
        None,
        None,
    ]
    assert all("current_minus_comparison" not in r for r in current + previous)


def test_complete_additive_money_aligns_both_sides_but_missing_coverage_stays_unknown():
    from compass_agent.semantic.execution import (
        group_totals,
        missing_group_is_zero,
        period_comparison,
    )

    def row(key, value, status="OK"):
        return {"group_key": key, "value": value, "status": status}

    info = {
        "aggregation": "additive_at_declared_grain",
        "metric": {"unit": "CNY_fen"},
        "binding": {
            "result_schema": {
                "properties": {
                    "unit": {"const": "CNY_fen"},
                    "status": {"enum": ["OK", "NO_FACTS", "SOURCE_DELAYED"]},
                }
            }
        },
    }
    assert missing_group_is_zero(info)
    assert not missing_group_is_zero({**info, "metric": {"unit": "piece"}})
    assert not missing_group_is_zero({**info, "aggregation": "non_additive"})
    a = [row("new", "198.01"), row(None, "0.12")]
    b = [row("old", "15.004"), row(None, "0.11")]
    result = period_comparison(a, b, empty_is_zero=True)
    assert [r["current_minus_comparison"] for r in result] == ["198.01", "0.01", "-15.004", None]
    assert (result[2]["current_value"], result[2]["comparison_value"]) == ("0", "15.004")
    assert result[2]["period_side"] == "comparison"
    windows = [
        {"start": "2026-02-08", "end": "2026-02-15", "group_by": "area"},
        {"start": "2026-02-01", "end": "2026-02-08", "group_by": "area"},
    ]
    assert [r["value"] for r in group_totals([a, b], windows, info)] == ["198.13", "15.114"]
    empty = [row(None, None, "NO_FACTS")]
    assert [r["value"] for r in group_totals([empty], windows[:1], info)] == ["0"]
    assert (
        period_comparison(empty, b, empty_is_zero=True)[1]["current_minus_comparison"] == "-15.004"
    )
    missing = [row(None, None, "SOURCE_DELAYED")]
    assert group_totals([missing], windows[:1], info) == []
    assert all(
        r["current_minus_comparison"] is None
        for r in period_comparison(a, missing, empty_is_zero=True)
    )
    prior_report = {
        **info,
        "binding": {
            "result_schema": {
                "properties": {
                    "unit": {"const": "CNY_fen"},
                    "status": {"enum": ["OK", "NO_REPORT_ROWS"]},
                }
            }
        },
    }
    assert not missing_group_is_zero(prior_report)


def test_invalid_filter_returns_its_registered_type_before_issuing_a_plan():
    from compass_agent.semantic.execution import parameter_feedback

    schema = {
        "type": "object",
        "properties": {
            "filters": {"type": "object", "properties": {"bin": {"type": "integer"}}},
            "group_by": {"enum": ["total", "facility"]},
        },
    }
    assert parameter_feedback(schema, {"filters": {"bin": 7}}) is None
    feedback = parameter_feedback(schema, {"filters": {"bin": "7"}})
    assert feedback is not None and "search_semantics" in feedback.pop("repair")
    assert feedback == {
        "status": "invalid_parameters",
        "field": "filters.bin",
        "filter_types": {"bin": "integer"},
        "group_by_options": ["total", "facility"],
    }


def test_receipt_scope_does_not_add_unrequested_comparisons():
    from compass_agent.semantic.receipts import with_scope

    w = {"start": "2026-01-01", "end": "2026-02-01", "group_by": "total"}
    metrics = [{"asset_id": "a", "name": "发出"}, {"asset_id": "b", "name": "收到"}]

    def observation(metric, value):
        return {
            "metric": metric,
            "time_range": [w],
            "source_scope_hash": "verified",
            "manifest_hash": "fixed",
            "as_of": "2026-03-01",
            "aggregation": "additive",
            "unit": "箱",
            "columns": ["group_key", "value", "status", "period_start", "period_end"],
            "rows": [["total", value, "OK", w["start"], w["end"]]],
        }

    a, b = observation(metrics[0], 3), observation(metrics[1], 7)
    answer = with_scope("发出 3 箱，收到 7 箱。", {"a": a, "b": b}, 8000)
    assert "发出 3 箱，收到 7 箱。" in answer and "取数口径" in answer and " 减 " not in answer


def test_reconcile_grouped_results_against_the_same_independent_total():
    from compass_agent.semantic.receipts import group_reconciliation_lines

    window = {"start": "2026-01-01", "end": "2026-02-01", "group_by": "total"}
    total = {
        "metric": {"asset_id": "fulfilled", "version_id": "v1", "name": "履约数"},
        "binding_hash": "binding",
        "source_scope_hash": "scope",
        "manifest_hash": "release",
        "as_of": "2026-02-04",
        "unit": "件",
        "aggregation": "additive_at_declared_grain",
        "time_range": [window],
        "columns": ["group_key", "value", "status", "period_start", "period_end"],
        "rows": [["total", "32", "OK", window["start"], window["end"]]],
    }

    def grouped(axis, value):
        return {
            **total,
            "time_range": [{**window, "group_by": axis}],
            "rows": [],
            "period_totals": [{"start": window["start"], "end": window["end"], "value": value}],
        }

    district = grouped("district", "32.0")
    lines = group_reconciliation_lines(
        {"all": total, "districts": district, "sites": grouped("site", "28")}
    )
    assert len(lines) == 2 and "核对结果一致" in lines[0] and "差额 -4" in lines[1]
    for patch in (
        {"binding_hash": "other"},
        {"source_scope_hash": "restricted"},
        {"as_of": "2026-02-05"},
        {"aggregation": "non_additive"},
        {"period_totals": []},
        {"time_range": [{**district["time_range"][0], "filters": {"zone": 1}}]},
    ):
        assert not group_reconciliation_lines({"all": total, "districts": {**district, **patch}})
    assert not group_reconciliation_lines(
        {"all": total, "a": district, "b": grouped("district", "30")}
    )


def test_previous_equal_window_preserves_half_open_dates_and_original_scope():
    from compass_agent.semantic.execution import previous_equal_window

    params = {
        "start": "2024-03-01",
        "end": "2024-03-08",
        "as_of": "2024-04-01T00:00:00Z",
        "filters": {"location": "north"},
        "group_by": "supplier",
    }
    assert previous_equal_window(params) == {**params, "start": "2024-02-23", "end": "2024-03-01"}
    assert previous_equal_window({**params, "start": "0001-01-01", "end": "0001-01-02"}) is None
    for end in ("2024-03-01", "2024-02-30"):
        with pytest.raises(ToolRejected, match="invalid_parameters"):
            previous_equal_window({**params, "end": end})


def test_result_confirmation_has_no_scope_or_author_supplied_by_caller():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        SemanticResultDecision.model_validate(
            {"result_ref": str(uuid4()), "action": "approve", "tenantId": str(uuid4())}
        )


def test_draft_preview_and_legacy_confirmation_use_the_recorded_source_fields():
    from compass_agent.semantic.draft_fields import confirmed_fields, result_preview
    from compass_agent.tools.business.discover_semantic_candidates import Input as Discover

    ref = uuid4()
    original = {
        "summary": "发货统计",
        "formula": "count(*)",
        "dimensions": ["warehouse"],
        "filters": ["shipped_at"],
    }
    suggestions = {
        "name": "出库件数",
        "filters": "warehouse = 1",
        "data_source": "other",
        "owner_scope_id": str(uuid4()),
    }
    tool_args = Discover.model_validate({"result_ref": str(ref), "fields": suggestions})
    fields = result_preview(original, tool_args.model_dump(exclude_none=True)["fields"], ref)
    assert fields["name"] == "出库件数" and fields["filters"] == ["shipped_at"]
    assert "data_source" not in fields and "owner_scope_id" not in fields
    edited = {**fields, "summary": "", "domain": "履约", "usage_scope_id": str(uuid4())}
    assert confirmed_fields(original, edited, ref) == edited
    repaired = confirmed_fields(
        original, {**edited, "data_source": "other", "filters": "warehouse = 1"}, ref
    )
    assert (
        repaired["filters"] == ["shipped_at"]
        and repaired["usage_scope_id"] == edited["usage_scope_id"]
    )
    assert "data_source" not in repaired


def test_explicit_save_needs_a_readable_definition_before_showing_a_form():
    from compass_agent.semantic.draft_fields import authored_preview

    ref = uuid4()
    receipt = {"proposal": {"fields": {"name": "cnt", "formula": "count(*)", "summary": "cnt汇总"}}}
    assert authored_preview(receipt, None, ref) is None
    fields = {
        "name": "签收件数",
        "summary": "按签收日统计期内签收的件数",
        "formula": "符合条件的包裹数量",
    }
    proposed = authored_preview(receipt, fields, ref)
    assert proposed and proposed["name"] == "签收件数"
    receipt["recommendation"] = {"fields": fields}
    existing = authored_preview(receipt, None, ref)
    assert existing and existing["formula"] == fields["formula"]


def test_cross_turn_save_prefers_the_previous_answer_over_an_unpublished_repeat(monkeypatch):
    import asyncio

    from compass_agent.semantic import service

    store = object.__new__(RegistryTools)
    store.journal = store.trusted = None
    monkeypatch.setattr(store, "open_result", lambda ref: (ref, {}))

    def current(ref, _kind):
        if ref != "unpublished":
            raise ToolRejected("semantic_reference_expired")
        return {}

    monkeypatch.setattr(store, "get", current)
    monkeypatch.setattr(store, "chosen_result", lambda: None)
    monkeypatch.setattr(service, "session_results", lambda *_: [{"result_ref": "prior"}])
    assert asyncio.run(store.requested_result({"result_ref": "unpublished"})) == "prior"
    assert asyncio.run(store.requested_result({"result_ref": "prior"})) == "prior"


def test_wire_result_and_decision_round_trip_in_strict_http_models():
    from compass_agent.schemas import SemanticResults
    from compass_agent.semantic.results import project

    identifier, asset = uuid4(), uuid4()
    row = SimpleNamespace(offer_id=identifier, status="saved")
    data = {
        "columns": ["件数"],
        "created_at": datetime.now(UTC).isoformat(),
        "saved": {"asset_id": str(asset)},
    }
    result = SemanticResults.model_validate({"items": [project(row, data)]}).model_dump(mode="json")
    assert result["items"][0]["draft_id"] == str(asset)
    assert (
        SemanticResultDecision.model_validate(
            {"result_ref": str(identifier), "action": "confirm"}
        ).result_ref
        == identifier
    )


def test_source_definition_extracts_typed_time_bounds_and_parameterizes_private_constants():
    from compass_agent.semantic.extraction import definition_from_query

    query = (
        "SELECT district, sum(CASE WHEN state='private-rule' THEN units ELSE 0 END) AS units "
        "FROM stock WHERE business_day BETWEEN DATE '2026-03-01' AND DATE '2026-03-31' "
        "GROUP BY district"
    )
    result = definition_from_query(query, ["district", "units"], "按地区汇总件数")
    by_position = definition_from_query(query.replace("GROUP BY district", "GROUP BY 1"), [], "")
    assert by_position["fields"]["dimensions"] == ["district"]
    assert result["fields"]["aggregation"] == "sum"
    assert result["fields"]["event_time"] == "business_day"
    assert result["parameter_examples"] == ["2026-03-01", "2026-03-31"]
    assert "private-rule" not in result["fields"]["formula"]
    assert (
        result["query_signature"]
        == definition_from_query(query.replace("2026-03", "2026-04"), [], "")["query_signature"]
    )
    assert (
        result["query_signature"]
        != definition_from_query(query.replace("private-rule", "another-rule"), [], "")[
            "query_signature"
        ]
    )


def test_set_query_receipt_preserves_projected_aggregates_without_literal_values():
    from compass_agent.semantic.extraction import definition_from_query, receipt_proposal

    sql = (
        "SELECT count(DISTINCT x) FROM a WHERE k='private' "
        "UNION ALL SELECT count(DISTINCT y) FROM b"
    )
    result = definition_from_query(sql, ["count"], "")
    assert result["fields"]["formula"] == "(count(DISTINCT x)) UNION ALL (count(DISTINCT y))"
    old = {"proposal": {"fields": {"formula": ""}}, "query": {"sql": sql}, "columns": ["count"]}
    assert receipt_proposal(old)["fields"]["formula"] == result["fields"]["formula"]


def test_only_nominated_novel_results_can_offer_saving(monkeypatch):
    from compass_agent.semantic import results

    store = SimpleNamespace(source_settings=None)
    raw = {"reusable": True, "definition_hash": "raw"}
    assert not results.invitation_eligibility([("raw", "confirmed", raw)], store, None, None)
    checked = []

    def equivalents(_settings, _trusted, _run, op, args):
        assert op == "equivalents"
        checked.extend(args["hashes"])
        return {"hashes": ["known"]}

    monkeypatch.setattr(results.client, "request", equivalents)

    def row(key, status):
        fields = {
            "name": "复购率",
            "summary": "在统计期内重复下单的客户占比",
            "formula": "重复下单人数 / 下单人数",
        }
        return key, status, {"definition_hash": key, "recommendation": {"fields": fields}}

    sample = [
        row("hidden", "dismissed"),
        row("known", "active"),
        row("new", "active"),
        row("other", "confirmed"),
        row("extra", "active"),
    ]
    assert results.invitation_eligibility(sample, store, None, None) == {"new", "other"}
    assert checked == ["known", "new", "other"]


def test_nomination_requires_an_authored_definition_and_a_reuse_case():
    from compass_agent.semantic.results import nominate

    assert nominate(None, {"result_ref": str(uuid4()), "fields": {"name": "status"}}) == {
        "status": "incomplete_definition"
    }


@pytest.mark.parametrize("status", ["unavailable_binding", "query_forbidden", "no_match"])
def test_resolution_returns_observations_without_ending_the_run(status):
    import asyncio
    from unittest.mock import Mock

    metric = {"asset_id": "visible", "version_id": "fixed", "executable": False}
    observation = {
        "status": status,
        **({"metric": metric} if status != "no_match" else {}),
    }
    svc = object.__new__(RegistryTools)
    svc.journal = SimpleNamespace(accepted_at=datetime.now(UTC), approved_proposals=[])
    svc.api = AsyncMock(return_value=observation)
    svc.choice = Mock(return_value=None)
    svc.record = Mock()
    svc.put = Mock()
    assert asyncio.run(svc.run("resolve", {"mention": "履约量"}, object())) == observation
    svc.put.assert_not_called()
    assert svc.record.call_count == int(status != "no_match")


def test_resolve_tool_requests_a_choice_only_if_context_has_not_selected_a_candidate():
    import asyncio

    from compass_agent.tools.business.resolve_semantics import SPEC

    choices = [
        {
            "asset_id": str(uuid4()),
            "version_id": str(uuid4()),
            "name": n,
            "summary": n,
            "unit": "元",
            "version": 2,
        }
        for n in ("成功支付金额", "已确认收入")
    ]

    async def registry(op, args):
        assert op == "resolve" and args["mention"] == "销售额"
        if args.get("metric_id") == choices[0]["asset_id"]:
            return {"status": "resolved", "metric": choices[0]}
        return {"status": "needs_clarification", "options": choices}

    service = SimpleNamespace(
        api=registry, put=lambda *_: None, choice=lambda _: None, journal=SimpleNamespace()
    )
    context: Any = SimpleNamespace(semantics=service)
    assert SPEC.context_factory is not None
    prepare = SPEC.context_factory(context).prepare_approval
    assert prepare is not None
    execution: Any = SimpleNamespace(run_id=uuid4(), step_id=uuid4(), call_id="resolve")

    async def request(args):
        return await prepare(args, execution)

    assert asyncio.run(request({"mention": "销售额", "metric_id": choices[0]["asset_id"]})) is None
    pending = asyncio.run(request({"mention": "销售额"}))
    assert pending is not None and pending.private["kind"] == "semantic_choice"
    assert [row["id"] for row in pending.private["options"]] == [m["asset_id"] for m in choices]


def test_comparison_reports_each_approved_definition_and_clock_once():
    from compass_agent.semantic.receipts import scope_lines

    def observation(identity, clock):
        return {
            "metric": {
                "name": "发货数",
                "version": 1,
                "asset_id": identity,
                "event_time": clock,
                "summary": "按审核定义计算",
                "known_at": "交接记录生效后可见",
                "formula": f"各货单取{clock}的最新有效修订",
                "exceptions": "修订作废时回到首报",
            },
            "time_range": [{"start": "2026-02-01", "end": "2026-02-10"}],
            "unit": "件",
        }

    posted = observation("dispatch", "出库交接时间")
    signed = observation("delivered", "收货人签收时间")
    text = "\n".join(scope_lines({"period1": posted, "period2": posted, "different": signed}))
    assert text.count("业务时间 出库交接时间") == 1 and text.count("业务时间 收货人签收时间") == 1
    assert text.count("交接记录生效后可见") == 2
    assert text.count("最新有效修订") == 2 and text.count("修订作废时回到首报") == 2


def test_source_recheck_retries_temporary_failure_and_immediately_honors_revocation(monkeypatch):
    from unittest.mock import Mock

    from compass_agent.context_runtime import source_access
    from compass_agent.models.errors import ModelError
    from compass_agent.schemas import TenantScope
    from compass_agent.semantic import service

    trusted = SimpleNamespace(
        scope=TenantScope(kind="tenant", tenant_id=uuid4(), workspace_id=uuid4())
    )
    check = Mock(
        side_effect=[
            TimeoutError(),
            True,
            ModelError("forbidden", 403),
            TimeoutError(),
            TimeoutError(),
        ]
    )
    monkeypatch.setattr(service, "read_sources", check)
    monkeypatch.setattr(source_access.time, "sleep", lambda _: None)
    refs = [{"kind": "semantic"}]
    assert source_access.allows(None, trusted, refs)
    assert not source_access.allows(None, trusted, refs)
    assert check.call_count == 3
    with pytest.raises(ModelError, match="source_authorization_unavailable"):
        source_access.allows(None, trusted, refs)
    assert check.call_count == 5


def test_custom_definition_is_returned_as_owner_input_without_a_formal_plan():
    import asyncio
    from unittest.mock import Mock

    from compass_agent.semantic import decisions

    private = {"kind": "semantic_choice", "callId": "call", "stepId": "step"}
    command = SimpleNamespace(custom_definition=" 按签收日去重计件 ")
    decisions.apply(None, None, None, private, command)
    svc = object.__new__(RegistryTools)
    svc.journal = SimpleNamespace(accepted_at=datetime.now(UTC), approved_proposals=[private])
    svc.api = AsyncMock()
    svc.put = Mock()
    result = asyncio.run(
        svc.run("resolve", {"mention": "履约量"}, SimpleNamespace(call_id="call", step_id="step"))
    )
    assert result == {
        "status": "user_definition",
        "confirmed_by": "current_user",
        "mention": "履约量",
        "definition": "按签收日去重计件",
    }
    svc.api.assert_not_awaited()
    svc.put.assert_not_called()


@pytest.mark.parametrize("selected", [False, True], ids=["single-result", "selected-from-several"])
def test_approved_draft_uses_original_receipt_and_owner_edits(monkeypatch, selected):
    import asyncio
    from unittest.mock import Mock

    from compass_agent.schemas import RunCompleted
    from compass_agent.semantic import results
    from compass_agent.semantic.clarification import continue_selected_result

    run, action, ref, epoch = (uuid4() for _ in range(4))
    fields = {"code": "weekly_shipments", "name": "每周发货数", "domain": "履约"}
    original = (
        {"origin": "selected-result", "result_ref": str(ref)}
        if selected
        else {"args": {"result_ref": str(ref)}}
    )
    approved = {**original, "kind": "semantic_draft", "actionId": str(action), "fields": fields}
    journal = SimpleNamespace(run_id=run, epoch=epoch, approved_proposals=[approved])
    service = SimpleNamespace(journal=journal, store=object(), trusted=object())
    decide = Mock()
    monkeypatch.setattr(results, "decide_result", decide)
    completed = asyncio.run(continue_selected_result(service))
    assert isinstance(completed, RunCompleted) and fields["name"] in completed.final_answer
    decide.assert_called_once_with(
        service.store,
        service.trusted,
        run,
        SemanticResultDecision(result_ref=ref, action="save", fields={**fields}),
        for_tool=True,
        action_id=action,
        epoch=epoch,
    )


def test_finance_scope_discovers_user_plugins_only_when_granted_in_registry_mode():
    from compass_agent.context_runtime.profiles import resolve_profile

    def available(registry, plugins):
        return resolve_profile(
            "finance",
            "business_qa",
            "SC-01",
            "query",
            available=("database_query", "resolve_semantics", "mcp_private_shipping"),
            plugins=plugins,
            registry=registry,
        ).tools

    assert available(False, True) == ("database_query",)
    assert available(True, False) == ("database_query", "resolve_semantics")
    assert "mcp_private_shipping" in available(True, True)


def test_dated_definition_and_cross_turn_save_follow_their_respective_routes(monkeypatch):
    import asyncio
    from typing import Any

    from compass_agent.execution_context import current_execution
    from compass_agent.runtime.task_state import TaskState
    from compass_agent.runtime.turn import RuntimeTurn
    from compass_agent.schemas import AnalysisFileNone, ChatMessage, RunCompleted
    from compass_agent.semantic import clarification

    done = RunCompleted(
        status="completed", finalAnswer="已处理", file=AnalysisFileNone(status="none")
    )
    continuation = AsyncMock(return_value=done)
    monkeypatch.setattr(clarification, "continue_selected_result", continuation)
    runner: Any = object.__new__(RuntimeTurn)
    runner.execute_tools = AsyncMock(return_value=done)
    state = TaskState(
        [ChatMessage(role="user", content="请解释 2026-03-01 至 2026-03-08 的统计定义。")]
    )
    journal: Any = SimpleNamespace(
        store=None, context_binding={"registry": {"pins": []}}, approved_proposals=[]
    )
    token = current_execution.set(journal)
    try:
        route = SimpleNamespace(deliverable="business_qa", task_type="指标解释")
        assert asyncio.run(runner.execute_turn(state, route, None, None, None, None)) == done
        state = TaskState([ChatMessage(role="user", content="本月履约量是多少？")])
        route.task_type = "查数查询"
        assert asyncio.run(runner.execute_turn(state, route, None, None, None, None)) == done
        route.deliverable = "existing"
        journal.approved_proposals = [{"kind": "semantic_result_choice"}]
        assert asyncio.run(runner.execute_turn(state, route, None, None, None, None)) == done
        continuation.assert_awaited_once()
        assert runner.execute_tools.await_count == 2
    finally:
        current_execution.reset(token)


def test_cancel_during_context_reads_drains_metadata_before_releasing_the_worker():
    import asyncio

    from compass_agent.context_runtime.model_step import finish_context_read
    from compass_agent.tools.contracts import PhaseCanceled

    async def exercise():
        entered, release = asyncio.Event(), asyncio.Event()

        async def read():
            entered.set()
            await release.wait()

        pending = asyncio.create_task(finish_context_read(read()))
        await entered.wait()
        pending.cancel()
        await asyncio.sleep(0)
        assert not pending.done()
        release.set()
        with pytest.raises(PhaseCanceled) as canceled:
            await pending
        assert canceled.value.cleanup_confirmed

    asyncio.run(exercise())


def test_known_at_limits_published_metrics_and_historical_queries():
    from datetime import UTC, datetime

    from compass_agent.semantic.service import effective_cutoff
    from compass_agent.tools.contracts import ToolRejected

    accepted = datetime(2026, 1, 2, tzinfo=UTC)
    cutoff = datetime(2025, 10, 9, 1, tzinfo=UTC)
    snapshot = {"knownAt": "2025-10-09T09:00:00+08:00"}
    assert effective_cutoff(accepted, None) == accepted
    assert effective_cutoff(accepted, snapshot) == cutoff
    assert effective_cutoff(cutoff, {"knownAt": accepted.isoformat()}) == cutoff
    earlier = datetime(2025, 10, 8, tzinfo=UTC)
    assert effective_cutoff(accepted, snapshot, earlier.isoformat()) == earlier
    for candidate in (accepted.isoformat(), "2025-10-08", "invalid"):
        with pytest.raises(ToolRejected, match="invalid_parameters"):
            effective_cutoff(accepted, snapshot, candidate)
    with pytest.raises(ToolRejected, match="semantic_source_unavailable"):
        effective_cutoff(accepted, {"knownAt": "2025-10-09"})


def test_metric_revision_rechecks_the_version_and_limits_owner_edits(monkeypatch):
    from unittest.mock import Mock

    from compass_agent.models.errors import ModelError
    from compass_agent.semantic import decisions

    store = SimpleNamespace(source_settings=object())
    run = SimpleNamespace(
        id=uuid4(), config_snapshot={"contextRuntime": {"registry": {"pins": []}}}
    )
    private = {
        "kind": "semantic_revision",
        "revision": {"asset_id": str(uuid4()), "expected_digest": "v1", "changes": {"unit": "件"}},
    }
    view = {
        "name": "收货量",
        "code": "receipts",
        "changes": [{"field": "unit", "before": "个", "after": "件"}],
    }
    api = Mock(return_value=view)
    monkeypatch.setattr(decisions, "request", api)

    def apply(edits):
        decisions.apply(
            store,
            run,
            object(),
            private,
            SimpleNamespace(choice_id=None, draft_edits=edits, custom_definition=None),
        )

    for invalid in [{"formula": "sum(0)"}, {"unit": "个"}, {"unit": " "}]:
        with pytest.raises(ModelError) as rejected:
            apply(invalid)
        assert rejected.value.status == 422
    apply({"unit": " 箱 "})
    assert private["revision"]["changes"] == {"unit": "箱"}
    api.side_effect = ModelError("proposal_conflict", 409)
    assert decisions.card(store, run, object(), private) == {"semanticKind": "revision"}
    with pytest.raises(ModelError) as stale:
        apply({})
    assert stale.value.status == 409


def test_registered_metric_does_not_block_a_new_detail_read():
    import asyncio

    from compass_agent.execution_context import current_execution
    from compass_agent.tools.builtin.database_query.handler import registration

    tool: Any = registration(())
    token = current_execution.set(SimpleNamespace(context_binding={"registry": {"pins": []}}))
    try:
        receipt = asyncio.run(
            tool.binding.handler({"op": "list_connections"}, SimpleNamespace(call_id="detail"))
        )
        assert receipt.payload == {"connections": []}
    finally:
        current_execution.reset(token)


def test_default_terms_keep_their_versions_beyond_the_tool_reference_budget(monkeypatch):

    from compass_agent.context_runtime import source_access
    from compass_agent.semantic import service

    run = SimpleNamespace(id=uuid4(), config_snapshot={"contextRuntime": {"registry": True}})
    message = SimpleNamespace(source_refs=[])
    monkeypatch.setattr(source_access, "assistant_message", lambda *_: message)
    store = SimpleNamespace(require_epoch=lambda *_: None)
    journal = SimpleNamespace(
        run_id=run.id, store=store, context_binding={"glossary": {"pins": ["p"]}}
    )
    registry = service.RegistryTools(journal, None)
    monkeypatch.setattr(
        service,
        "record_sources",
        lambda _, refs: source_access.record_sources_locked(store, None, run, refs, None),
    )
    terms = [
        {
            "kind": "term",
            "asset_id": str(uuid4()),
            "version_id": str(uuid4()),
            "context_reason": "默认术语",
        }
        for _ in range(70)
    ]
    registry.record_page(terms)
    registry.record_page(terms)
    assert len(message.source_refs) == 70
    assert all(
        r["kind"] == "semantic" and r["default_term"] and r["pins"] == ["p"]
        for r in message.source_refs
    )
    assert {r["version_id"] for r in message.source_refs} == {term["version_id"] for term in terms}


def test_registry_pin_distinguishes_an_empty_release_from_unavailable_permissions(monkeypatch):
    from compass_agent.models.errors import ModelError
    from compass_agent.semantic import client

    def denied(*args):
        raise ModelError("forbidden", 403)

    monkeypatch.setattr(client, "request", denied)
    assert client.pin_manifest(None, None, None, "registry") == {
        "registryIssue": {"status": "unavailable", "code": "forbidden"}
    }
    monkeypatch.setattr(client, "request", lambda *args: {"releases": [], "manifest_hash": "fixed"})
    assert client.pin_manifest(None, None, None, "glossary") == {
        "glossary": {"manifest": "fixed", "pins": []}
    }


def test_recommendations_are_idempotent_bounded_and_preserve_source_receipts(monkeypatch):
    from contextlib import nullcontext
    from unittest.mock import Mock

    from compass_agent.semantic import results
    from compass_agent.semantic.extraction import receipt_proposal

    refs = [str(uuid4()) for _ in range(5)]
    rows = [
        SimpleNamespace(
            offer_id=ref,
            status="active",
            private_payload={
                "proposal": {"fields": {"name": "cnt", "formula": "count(*)"}},
                "complete": True,
                "refs": ["witness"],
                "definition_hash": ref,
            },
        )
        for ref in refs
    ]
    tx, store = Mock(), Mock()
    tx.scalars.return_value.all.return_value = rows
    store.transaction.side_effect = lambda: nullcontext(tx)
    store.lock.return_value = (None, Mock())
    store.private.open.side_effect = store.private.seal.side_effect = lambda _run, _kind, data: data
    service = SimpleNamespace(
        store=store,
        journal=SimpleNamespace(run_id=uuid4(), epoch=uuid4()),
        trusted=SimpleNamespace(actor_user_id=uuid4()),
        open_result=lambda ref: (ref, rows[refs.index(ref)].private_payload),
    )
    monkeypatch.setattr(results, "allows", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(results.client, "request", lambda *_args: {"hashes": []})

    def offer(index, name):
        return results.nominate(
            service,
            {
                "result_ref": refs[index],
                "fields": {
                    "name": name,
                    "summary": "运输时长按签收日期归期",
                    "formula": [
                        "平均（签收时间 − 发运时间）",
                        "中位数（签收时间 − 发运时间）",
                        "上四分位数（签收时间 − 发运时间）",
                        "城区平均配送时长",
                        "平均配送时长",
                    ][index],
                    "domain": "履约",
                    "unit": "天",
                },
                "reuse_reason": "按月评估承运商时效",
            },
        )["status"]

    assert offer(0, "配送时长") == offer(0, "配送时长") == "recommended"
    assert offer(1, "配送时长") == "already_considered"
    assert offer(1, "配送时长中位数") == offer(2, "配送时长上四分位数") == "recommended"
    assert offer(3, "城区配送时长") == "not_recommended"
    assert rows[0].private_payload["proposal"]["fields"]["name"] == "cnt"
    assert receipt_proposal(rows[0].private_payload)["fields"]["name"] == "配送时长"
    rows[4].private_payload["complete"] = False
    assert offer(4, "未完整读取") == "incomplete_result"
    monkeypatch.setattr(results, "allows", lambda *_args, **_kwargs: False)
    with pytest.raises(results.ModelError) as denied:
        offer(0, "配送时长")
    assert denied.value.code == "source_access_denied"


def test_large_result_reauthorization_hashes_all_rows_not_preview(tmp_path):
    import asyncio

    from compass_agent.tools.builtin.database_query.result_output import result_fingerprint
    from compass_agent.tools.result_store import ToolResultStore

    rows = [(i, f"商品{i}") for i in range(255)]
    files = ToolResultStore(tmp_path)
    with files.writer(uuid4(), uuid4(), uuid4(), "query", columns=("id", "name")) as writer:
        for row in rows:
            writer.row(row)
        manifest = writer.finish()
    original = DatabaseResult(
        "original", ("id", "name"), tuple(rows[:5]), True, context_result=manifest
    )

    class Stream:
        async def __aenter__(self):
            self.pending = list(rows)
            return self

        async def __aexit__(self, *args):
            pass

        async def execute(self, query):
            pass

        async def fetchmany(self, size):
            import json

            batch, self.pending = self.pending[:size], self.pending[size:]
            return [(json.dumps(dict(zip(("id", "name"), row, strict=True))),) for row in batch]

    cursor = SimpleNamespace(connection=SimpleNamespace(cursor=lambda **kwargs: Stream()))
    control = SimpleNamespace(require_running=lambda: None)
    verified = asyncio.run(result_fingerprint(cursor, "SELECT id, name FROM products", control))
    assert open_digest(original) == open_digest(verified)
    rows[-1] = (254, "已变化")
    changed = asyncio.run(result_fingerprint(cursor, "SELECT id, name FROM products", control))
    assert open_digest(original) != open_digest(changed)
