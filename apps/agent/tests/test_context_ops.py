"""Pure replay, projection failure isolation and retention contract regressions."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from threading import Event
from types import SimpleNamespace
from uuid import uuid4

from compass_agent.context_ops.compiler import compile_candidates, freeze
from compass_agent.context_ops.entities import ContextBadcase, ContextSnapshot
from compass_agent.context_ops.maintenance import purge_expired
from compass_agent.context_ops.recorder import ContextOpsRecorder
from compass_agent.context_ops.replay import compare
from compass_agent.context_runtime.pipeline import collect
from compass_agent.schemas import ChatMessage, ContextReplayOverride
from sqlalchemy import Table, create_engine, select
from sqlalchemy.orm import Session


def frozen():
    items = collect(
        [
            ChatMessage(role="user", content="历史问题"),
            ChatMessage(role="assistant", content="历史答复"),
            ChatMessage(role="user", content="当前问题"),
        ]
    )
    items[1] = replace(items[1], trust="dictionary")
    selected, _, _, _, _ = compile_candidates(items, 10000)
    return freeze(items, selected, overhead=0, limit=10000)


def test_replay_is_deterministic_uses_real_selection_and_preserves_dictionary():
    payload = frozen()
    one = compare(payload, ContextReplayOverride())
    assert one == compare(payload, ContextReplayOverride())
    assert one.baseline_matches_snapshot
    assert one.token_delta == 0
    limit = payload["candidates"][-1]["byte_bound"]
    changed = compare(payload, ContextReplayOverride(max_input_tokens=limit))
    assert changed.newly_omitted_ids == ["0", "1"]
    assert not changed.candidate.dictionary_preserved
    protected = compare(
        payload, ContextReplayOverride(compaction_threshold=0.1, protect_dictionary=True)
    )
    assert protected.candidate.dictionary_preserved


def test_projection_queue_is_bounded_and_database_failure_never_escapes(monkeypatch):
    started, release = Event(), Event()
    recorder = ContextOpsRecorder(None, SimpleNamespace())
    recorder.queue.maxsize = 1

    def fail(*args):
        started.set()
        assert release.wait(2)
        raise OSError("sensitive failure must not be emitted")

    monkeypatch.setattr(recorder, "record", fail)
    receipt = {"contextOps": {"frozen": frozen(), "security_events": []}}
    recorder.submit(None, uuid4(), uuid4(), receipt)
    assert started.wait(1)
    recorder.submit(None, uuid4(), uuid4(), receipt)
    recorder.submit(None, uuid4(), uuid4(), receipt)
    assert recorder.dropped == 1
    release.set()
    recorder.flush()
    recorder.close()
    assert recorder.errors == 2


def test_retention_preserves_expired_badcase_snapshot_and_deletes_unreferenced():
    engine = create_engine("sqlite://")
    for table in (ContextSnapshot.__table__, ContextBadcase.__table__):
        assert isinstance(table, Table)
        table.create(engine)
    now = datetime.now(UTC)
    tenant, workspace, run, step, actor = (uuid4() for _ in range(5))
    with Session(engine) as tx, tx.begin():
        for ref, expires in (
            ("old", now - timedelta(days=1)),
            ("pinned", now - timedelta(days=1)),
            ("live", now + timedelta(days=1)),
        ):
            tx.add(
                ContextSnapshot(
                    snapshot_ref=ref,
                    tenant_id=tenant,
                    workspace_id=workspace,
                    run_id=run,
                    step_id=step,
                    content_sha256="0" * 64,
                    min_trust_level="observation",
                    access_scope_json={},
                    data_classification="internal",
                    ciphertext=b"opaque",
                    expires_at=expires,
                    created_at=now,
                )
            )
        tx.add(
            ContextBadcase(
                id=uuid4(),
                tenant_id=tenant,
                workspace_id=workspace,
                created_by=actor,
                run_id=run,
                step_id=step,
                snapshot_ref="pinned",
                scene_id="test",
                root_cause_category="source_data",
                title="case",
                policy_digest="0" * 64,
                prompt_generation=1,
                baseline_metrics_json={},
                status="open",
                created_at=now,
            )
        )
    assert purge_expired(engine, now=now) == 1
    with Session(engine) as tx:
        assert set(tx.scalars(select(ContextSnapshot.snapshot_ref))) == {"pinned", "live"}


def test_summary_partitions_permission_domains_before_calling_model(monkeypatch):
    import asyncio

    from compass_agent.context_runtime import compaction
    from compass_agent.context_runtime.compaction import SummarySnapshot

    source_a, source_b = {"binding": "finance", "revision": 1}, {"binding": "hr", "revision": 2}
    groups = [
        SimpleNamespace(id="a", source_deps=(source_a,)),
        SimpleNamespace(id="public", source_deps=()),
        SimpleNamespace(id="b", source_deps=(source_b,)),
    ]
    seen = []

    async def summarize(*args, previous):
        material = args[7]
        seen.append(tuple(g.id for g in material))
        return SummarySnapshot(
            text="summary-" + material[0].id,
            selected_ids=tuple(g.id for g in material),
            covered_ranges=(),
            query_hash="q",
            prompt_version="1",
            source_deps=tuple(r for g in material for r in g.source_deps),
            attempt_refs=(),
            pruned_bytes=0,
        )

    monkeypatch.setattr(compaction, "_summarize_segment", summarize)
    result = asyncio.run(
        compaction.summarize(None, None, None, None, None, None, None, groups, "q")
    )
    assert seen == [("a",), ("public",), ("b",)]
    assert len(result.segments) == 3
    assert result.min_trust_level == "observation"
    assert result.source_deps == (source_a, source_b)


def test_tool_flow_uses_business_outcome_even_when_journal_step_completed():
    from collections import defaultdict

    from compass_agent.context_ops.analytics import Analytics, search_turns

    run_id, step_id = uuid4(), uuid4()
    run = SimpleNamespace(
        id=run_id,
        public_view={
            "objects": {
                status: {
                    "kind": "execution.node",
                    "value": {
                        "id": str(uuid4()),
                        "stepId": str(step_id),
                        "kind": "tool",
                        "status": status,
                        "origin": {"toolName": "database_query"},
                        "startedAtMs": 1000,
                        "endedAtMs": 1050,
                        "detail": {"input": "private SQL must not leave this layer"},
                    },
                }
                for status in ("empty", "failed")
            }
        },
    )
    data = tuple(defaultdict(list) for _ in range(4))
    data[1][run_id].append(SimpleNamespace(kind="tool", status="completed"))
    nodes = Analytics(None, None).nodes(run, data)
    assert {node.status for node in nodes} == {"empty", "failed"}
    assert {node.label for node in nodes} == {"database_query"}
    assert all(node.duration_ms == 50 and node.track == "tool" for node in nodes)
    assert "private SQL" not in str([node.model_dump() for node in nodes])
    assert search_turns(run, data[1][run_id]) == 1
    run.status = "completed"
    _, failed, empty, truncated, reasons, stage = Analytics(None, None).inspect(run, data)
    assert (len(failed), empty, truncated) == (1, 1, 0)
    assert reasons == ["empty_result", "node_failed"]
    assert stage == "research"


def test_langfuse_status_follows_configuration_and_export_health():
    from compass_agent.context_ops.analytics import Analytics

    settings = SimpleNamespace(
        compass_observability_enabled=True, compass_langfuse_base_url="http://lf.local/"
    )
    health = {"configured": True, "last_export": "success"}
    observer = SimpleNamespace(status=lambda: health)
    analytics = Analytics(SimpleNamespace(source_settings=settings), None, observation=observer)
    assert analytics.langfuse() == ("connected", "http://lf.local")
    health["last_export"] = "failure"
    assert analytics.langfuse() == ("unavailable", None)
    settings.compass_observability_enabled = False
    assert analytics.langfuse() == ("disabled", None)


def test_scene_meta_maps_chinese_category_and_sub_scene_names():
    from compass_agent.context_ops.analytics import scene_meta

    assert scene_meta("SC-01") == ("business_channel", "经营与渠道", "经营指标快查")
    assert scene_meta("SC-03") == ("business_channel", "经营与渠道", "销售下降诊断")
    assert scene_meta("unclassified") == ("unclassified", "未识别 / 兜底", "未分类")
    assert scene_meta(None) == (None, None, None)


def test_terminal_outcomes_follow_recorded_model_refusal_and_artifact_types():
    from compass_agent.context_ops.analytics import terminal_outcome

    run = SimpleNamespace(status="failed", public_view={})
    assert terminal_outcome(run, [SimpleNamespace(error_code="model_timeout")]) == "failed"
    assert terminal_outcome(run, [SimpleNamespace(error_code="model_refused")]) == "safety_refusal"
    run.status = "waiting"
    assert terminal_outcome(run, []) == "hitl"
    run.status = "completed"
    assert terminal_outcome(run, []) == "text"
    run.public_view = {"objects": {"a": {"kind": "artifact", "value": {"mediaType": "text/csv"}}}}
    assert terminal_outcome(run, []) == "artifact"
    run.public_view["objects"]["a"]["value"]["mediaType"] = "image/png"
    assert terminal_outcome(run, []) == "chart"
