"""Content-free analytics over tenant-scoped runtime rows; no business-source probes."""

import re
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from statistics import mean
from typing import Literal
from urllib.parse import quote
from uuid import UUID

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from compass_agent import schemas as dto
from compass_agent.context_ops.entities import ContextDecisionSummary, ContextSecurityEvent
from compass_agent.context_security.redactor import redact
from compass_agent.models.entities import ModelCall
from compass_agent.models.errors import ModelError
from compass_agent.observability.privacy import tool_label
from compass_agent.routing.dictionary import catalog
from compass_agent.runtime.entities import AgentRun, AgentSession, AgentStep

# Events shown as "security interceptions" on the dashboard and its trend line.
type AnomalyCode = Literal["run_failed", "empty_result", "compaction_truncated", "node_failed"]
SECURITY_EVENTS = frozenset(
    {"injection_blocked", "injection_stripped", "tenant_mismatch_rejected", "pii_redacted"}
)


def scene_meta(scene_id: str | None) -> tuple[str | None, str | None, str | None]:
    """Return (category_id, category_name, scene_name) from the routing dictionary."""
    if not scene_id:
        return None, None, None
    if scene_id == "unclassified":
        return "unclassified", "未识别 / 兜底", "未分类"
    entry = catalog()[1].get(scene_id)
    if entry is None:
        return None, None, None
    return entry["category_id"], entry["category_name"], entry["name"]


def aware(value):
    return value.replace(tzinfo=UTC) if value and value.tzinfo is None else value


def milliseconds(start, end):
    return (
        max(0, round((aware(end) - aware(start)).total_seconds() * 1000)) if start and end else None
    )


def distribution(name, values, *, scene_id: str | None = None):
    values = sorted(v for v in values if v is not None)

    def percentile(q):
        if not values:
            return None
        index = (len(values) - 1) * q
        low = int(index)
        return values[low] + (values[min(low + 1, len(values) - 1)] - values[low]) * (index - low)

    return dto.ContextDuration(
        name=name,
        count=len(values),
        p50_ms=percentile(0.5),
        p95_ms=percentile(0.95),
        avg_ms=mean(values) if values else None,
        scene_id=scene_id,
    )


def counts(values):
    return [
        dto.ContextCount(name=str(k), count=v)
        for k, v in sorted(values.items(), key=lambda kv: (-kv[1], str(kv[0])))
    ]


def tool_nodes(run):
    return [
        obj["value"]
        for obj in run.public_view.get("objects", {}).values()
        if obj.get("kind") == "execution.node" and obj.get("value", {}).get("kind") == "tool"
    ]


def search_turns(run, steps):
    tools = tool_nodes(run)
    if tools:
        return len(
            {
                tool.get("parentId") or tool.get("stepId") or tool["id"]
                for tool in tools
                if any(
                    word in (tool.get("origin") or {}).get("toolName", "")
                    for word in ("search", "query", "retriev", "read_document")
                )
            }
        )
    return len(
        {
            step.parent_step_id or step.id
            for step in steps
            if step.kind == "tool"
            and any(
                word in str(step.definition_snapshot.get("name", step.node_key))
                for word in ("search", "query", "retriev", "read_document", "database:")
            )
        }
    )


def terminal_outcome(run, calls):
    artifacts = [
        obj for obj in run.public_view.get("objects", {}).values() if obj.get("kind") == "artifact"
    ]
    if run.status == "failed" and any(call.error_code == "model_refused" for call in calls):
        return "safety_refusal"
    if run.status == "waiting":
        return "hitl"
    if run.status == "completed":
        if any(obj.get("value", {}).get("mediaType") == "image/png" for obj in artifacts):
            return "chart"
        return "artifact" if artifacts else "text"
    return run.status


def phase(step, summary=None):
    if step.kind == "classifier":
        return "identify"
    if step.kind in {"ctx_select", "ctx_compact"}:
        return "context"
    if summary and summary.phase == "generate":
        return "answer"
    if step.kind == "model" and summary and summary.phase == "identify":
        return "identify"
    return "research" if step.kind in {"tool", "workflow", "model"} else "context"


def visible_runs(context, administrator):
    if context.scope.kind != "tenant":
        raise ModelError("forbidden", 403)
    query = (
        select(AgentRun, AgentSession)
        .join(AgentSession, AgentRun.session_id == AgentSession.id)
        .where(
            AgentSession.tenant_id == context.scope.tenant_id,
            AgentSession.workspace_id == context.scope.workspace_id,
            AgentSession.deleted_at.is_(None),
        )
    )
    if not administrator:
        query = query.where(AgentSession.owner_user_id == context.actor_user_id)
    return query


def require_run(tx, context, administrator, run_id):
    row = tx.execute(visible_runs(context, administrator).where(AgentRun.id == run_id)).first()
    if row is None:
        raise ModelError("forbidden", 403)
    return row


class Analytics:
    def __init__(self, store, context, administrator=False, observation=None):
        self.store, self.context, self.administrator = store, context, administrator
        self.observation = observation

    def scope(self, model):
        return (
            model.tenant_id == self.context.scope.tenant_id,
            model.workspace_id == self.context.scope.workspace_id,
        )

    def query(self, filters):
        query = visible_runs(self.context, self.administrator)
        days = {"24h": 1, "7d": 7, "30d": 30}[filters.window]
        query = query.where(AgentRun.created_at >= datetime.now(UTC) - timedelta(days=days))
        if filters.actor_id:
            if not self.administrator and filters.actor_id != self.context.actor_user_id:
                raise ModelError("forbidden", 403)
            query = query.where(AgentSession.owner_user_id == filters.actor_id)
        if filters.session_id:
            query = query.where(AgentSession.id == filters.session_id)
        if filters.status:
            query = query.where(AgentRun.status == filters.status)
        summary = select(ContextDecisionSummary.id).where(
            *self.scope(ContextDecisionSummary), ContextDecisionSummary.run_id == AgentRun.id
        )
        if filters.scene_id:
            summary = summary.where(ContextDecisionSummary.scene_id == filters.scene_id)
        if filters.policy_digest:
            summary = summary.where(ContextDecisionSummary.policy_digest == filters.policy_digest)
        if filters.scene_id or filters.policy_digest:
            query = query.where(summary.exists())
        if filters.has_security_event is not None:
            security = select(ContextSecurityEvent.id).where(
                *self.scope(ContextSecurityEvent),
                ContextSecurityEvent.run_id == AgentRun.id,
                ContextSecurityEvent.action.in_(("block", "deny", "strip", "redact")),
            )
            query = query.where(
                security.exists() if filters.has_security_event else ~security.exists()
            )
        return query.order_by(AgentRun.created_at.desc(), AgentRun.id.desc())

    def dataset(self, tx, rows):
        ids = [run.id for run, _ in rows]
        summaries, steps, calls, events = (defaultdict(list) for _ in range(4))
        if ids:
            for entry in tx.scalars(
                select(ContextDecisionSummary)
                .where(*self.scope(ContextDecisionSummary), ContextDecisionSummary.run_id.in_(ids))
                .order_by(ContextDecisionSummary.created_at)
            ):
                summaries[entry.run_id].append(entry)
            for entry in tx.scalars(
                select(AgentStep).where(AgentStep.run_id.in_(ids)).order_by(AgentStep.ordinal)
            ):
                steps[entry.run_id].append(entry)
            for entry in tx.scalars(
                select(ModelCall).where(ModelCall.run_id.in_(ids), *self.scope(ModelCall))
            ):
                calls[entry.run_id].append(entry)
            for entry in tx.scalars(
                select(ContextSecurityEvent).where(
                    *self.scope(ContextSecurityEvent), ContextSecurityEvent.run_id.in_(ids)
                )
            ):
                events[entry.run_id].append(entry)
        return summaries, steps, calls, events

    def view(self, row, dataset):
        run, session = row
        summaries, steps, calls, _ = dataset
        material = summaries[run.id]
        candidates = sum(s.candidate_count for s in material)
        usage = [c.usage for c in calls[run.id]]
        tokens = (
            sum((u.get("input_tokens") or 0) + (u.get("output_tokens") or 0) for u in usage if u)
            if usage
            and all(
                u and u.get("input_tokens") is not None and u.get("output_tokens") is not None
                for u in usage
            )
            else None
        )
        scene = next((s.scene_id for s in reversed(material) if s.scene_id != "unclassified"), None)
        _, category_name, scene_name = scene_meta(scene)
        return dto.ContextRunView(
            run_id=run.id,
            session_id=session.id,
            actor_id=session.owner_user_id,
            session_title=redact(session.title)[0],
            status=run.status,
            scene_id=scene,
            scene_name=scene_name,
            category_name=category_name,
            created_at=run.created_at,
            step_count=len(steps[run.id]),
            duration_ms=milliseconds(run.started_at or run.created_at, run.finished_at),
            total_tokens=tokens,
            token_upper_bound=sum(s.tokens_after_compaction for s in material)
            if material
            else None,
            omission_rate=sum(s.omitted_count for s in material) / candidates
            if candidates
            else None,
            compaction_applied=any(s.compaction_applied for s in material),
            security_blocked_count=sum(s.security_blocked_count for s in material),
            trace_id=next((s.trace_id for s in reversed(material) if s.trace_id), None),
            historical=not material,
        )

    def filtered_dataset(self, tx, filters):
        rows = list(tx.execute(self.query(filters)).all())
        data = self.dataset(tx, rows)
        if filters.failed_stage:
            rows = [
                row
                for row in rows
                if any(
                    node.phase == filters.failed_stage
                    and node.status in {"failed", "blocked", "error"}
                    for node in self.nodes(row[0], data)
                )
            ]
            data = self.dataset(tx, rows)
        return rows, data

    def list_runs(self, filters):
        with Session(self.store.engine) as tx:
            rows, data = self.filtered_dataset(tx, filters)
            if filters.failed_stage:
                rows = [
                    row
                    for row in rows
                    if any(
                        n.phase == filters.failed_stage
                        and n.status in {"failed", "blocked", "error"}
                        for n in self.nodes(row[0], data)
                    )
                ]
            if filters.cursor:
                index = next((i for i, row in enumerate(rows) if row[0].id == filters.cursor), None)
                if index is None:
                    raise ModelError("cursor_invalid", 422)
                rows = rows[index + 1 :]
            page = rows[: filters.limit]
            return dto.ContextRuns(
                items=[self.view(row, data) for row in page],
                next_cursor=page[-1][0].id if len(rows) > len(page) else None,
                administrator=self.administrator,
            )

    def nodes(self, run, dataset):
        summaries, steps, calls, _ = dataset
        by_step = {s.step_id: s for s in summaries[run.id]}
        by_call = {c.call_id: c for c in calls[run.id]}
        nodes = []
        public_tools = tool_nodes(run)
        for step in steps[run.id]:
            if step.kind == "tool" and public_tools:
                continue
            summary = by_step.get(step.id)
            # User-controlled tool names are reduced to the registered catalog label.
            tool = None
            if step.kind == "tool":
                names = [
                    step.definition_snapshot.get("name", ""),
                    step.definition_snapshot.get("tool", ""),
                    *re.split(r"[/():]", step.node_key),
                ]
                tool = next(
                    (tool_label(n) for n in names if tool_label(n) != "external"),
                    "database_query" if "/database:" in step.node_key else "external",
                )
            label = tool or {
                "classifier": "意图识别",
                "model": "模型调用",
                "ctx_select": "历史选择",
                "ctx_compact": "摘要压缩",
            }.get(step.kind, step.kind)
            call = by_call.get(summary.call_id) if summary else None
            usage = call.usage if call else None
            tokens = (
                (usage.get("input_tokens") or 0) + (usage.get("output_tokens") or 0)
                if usage
                and usage.get("input_tokens") is not None
                and usage.get("output_tokens") is not None
                else None
            )
            nodes.append(
                dto.ContextFlowNode(
                    id=str(step.id),
                    step_id=step.id,
                    label=label,
                    track="tool"
                    if step.kind in {"tool", "workflow"}
                    else "model"
                    if step.kind in {"model", "classifier", "ctx_select", "ctx_compact"}
                    else "input",
                    phase=phase(step, summary),
                    status=step.status,
                    started_at=step.created_at,
                    duration_ms=milliseconds(step.created_at, step.finished_at),
                    ordinal=step.ordinal,
                    tokens=tokens,
                    error_code=call.error_code if call else None,
                )
            )
        for index, tool in enumerate(public_tools):
            origin = tool.get("origin") or {}
            started, ended = tool["startedAtMs"], tool.get("endedAtMs")
            nodes.append(
                dto.ContextFlowNode(
                    id=tool["id"],
                    step_id=UUID(tool.get("stepId") or tool["id"]),
                    label=tool_label(origin.get("toolName") or "external"),
                    track="tool",
                    phase="research",
                    status=tool["status"],
                    started_at=datetime.fromtimestamp(started / 1000, UTC),
                    duration_ms=max(0, ended - started) if ended is not None else None,
                    ordinal=index + 1,
                    tokens=None,
                )
            )
        for summary in summaries[run.id]:
            nodes.append(
                dto.ContextFlowNode(
                    id="context:" + str(summary.id),
                    step_id=summary.step_id,
                    label="上下文安全与编译",
                    track="input",
                    phase="context",
                    status=summary.status,
                    started_at=summary.created_at,
                    duration_ms=summary.duration_ms,
                    ordinal=next((s.ordinal for s in steps[run.id] if s.id == summary.step_id), 0),
                    tokens=summary.tokens_after_compaction,
                    snapshot_ref=summary.snapshot_ref,
                    policy_digest=summary.policy_digest,
                    prompt_generation=summary.prompt_generation,
                    semantic_version=summary.semantic_version,
                    security_blocked_count=summary.security_blocked_count,
                    redacted_count=summary.redacted_count,
                    funnel=summary.funnel_json,
                    source_breakdown=summary.source_breakdown_json,
                    decisions=summary.decisions_json.get("items", [])
                    if isinstance(summary.decisions_json, dict)
                    else summary.decisions_json,
                    toolAssembly=summary.decisions_json.get("toolAssembly")
                    if isinstance(summary.decisions_json, dict)
                    else None,
                )
            )
        return sorted(nodes, key=lambda n: (aware(n.started_at), n.ordinal, n.id))

    def flow(self, run_id):
        with Session(self.store.engine) as tx:
            row = require_run(tx, self.context, self.administrator, run_id)
            data = self.dataset(tx, [row])
            view = self.view(row, data)
            settings = self.store.source_settings
            url = None
            if (
                settings.compass_observability_enabled
                and settings.compass_langfuse_base_url
                and view.trace_id
            ):
                url = (
                    settings.compass_langfuse_base_url.rstrip("/")
                    + "/trace/"
                    + quote(view.trace_id, safe="")
                )
            export_status = self.observation.status() if self.observation else {}
            unavailable = export_status.get("last_export") == "failure"
            if unavailable or export_status.get("configured") is False:
                url = None
            return dto.ContextRunFlow(
                run=view,
                nodes=self.nodes(row[0], data),
                langfuse_url=url,
                langfuse_reason="Langfuse 导出暂时不可用，本地观测仍可查看"
                if unavailable
                else None
                if url
                else "未启用本地 Langfuse 导出"
                if not settings.compass_observability_enabled
                else "本次运行未记录可跳转的 Trace",
            )

    def inspect(self, run, data):
        """Classify one run for anomaly statistics from public node outcomes only."""
        nodes = self.nodes(run, data)
        failed = [n for n in nodes if n.status in {"failed", "blocked", "error"}]
        empty = sum(n.track == "tool" and n.status == "empty" for n in nodes)
        truncated = sum(bool(s.compaction_applied and s.omitted_count) for s in data[0][run.id])
        checks: tuple[tuple[AnomalyCode, object], ...] = (
            ("run_failed", run.status == "failed"),
            ("empty_result", empty),
            ("compaction_truncated", truncated),
            ("node_failed", failed),
        )
        reasons: list[AnomalyCode] = [code for code, hit in checks if hit]
        stage = (
            failed[0].phase
            if failed
            else "research"
            if empty
            else "context"
            if truncated
            else "answer"
            if run.status == "failed"
            else None
        )
        return nodes, failed, empty, truncated, reasons, stage

    def langfuse(self):
        settings = getattr(self.store, "source_settings", None)
        status = self.observation.status() if self.observation else {}
        base = getattr(settings, "compass_langfuse_base_url", None)
        if (
            not getattr(settings, "compass_observability_enabled", False)
            or not base
            or status.get("configured") is False
        ):
            return "disabled", None
        if status.get("last_export") == "failure":
            return "unavailable", None
        return "connected", base.rstrip("/")

    def trend(self, rows, data, flagged, window):
        count, size = {
            "24h": (6, timedelta(hours=4)),
            "7d": (7, timedelta(days=1)),
            "30d": (10, timedelta(days=3)),
        }[window]
        start = datetime.now(UTC) - count * size
        points = []
        for index in range(count):
            low = start + index * size
            high = low + size
            bucket = [
                row
                for row in rows
                if aware(row[0].created_at) >= low
                and (index == count - 1 or aware(row[0].created_at) < high)
            ]
            views = [self.view(row, data) for row in bucket]
            sessions = Counter(v.session_id for v in views)
            records = [s for row in bucket for s in data[0][row[0].id]]
            candidate = sum(s.candidate_count for s in records)
            points.append(
                dto.ContextTrendPoint(
                    start=low,
                    run_count=len(views),
                    session_count=len(sessions),
                    omission_rate=sum(s.omitted_count for s in records) / candidate
                    if candidate
                    else None,
                    p50_ms=distribution(
                        "run", [v.duration_ms for v in views if v.duration_ms is not None]
                    ).p50_ms,
                    anomaly_rate=sum(row[0].id in flagged for row in bucket) / len(bucket)
                    if bucket
                    else None,
                    security_event_count=sum(
                        e.hit_count
                        for row in bucket
                        for e in data[3][row[0].id]
                        if e.event_type in SECURITY_EVENTS
                    ),
                    avg_turns=mean(sessions.values()) if sessions else None,
                    completion_rate=sum(v.status == "completed" for v in views) / len(views)
                    if views
                    else None,
                    avg_search_turns=mean(
                        search_turns(row[0], data[1][row[0].id]) for row in bucket
                    )
                    if bucket
                    else None,
                )
            )
        return points

    def overview(self, filters):
        with Session(self.store.engine) as tx:
            rows, data = self.filtered_dataset(tx, filters)
            summaries, steps, calls, events = data
            views = [self.view(row, data) for row in rows]
            session_turns = Counter(v.session_id for v in views)
            records = [s for group in summaries.values() for s in group]
            security = [e for group in events.values() for e in group]
            candidate = sum(s.candidate_count for s in records)
            durations = [v.duration_ms for v in views if v.duration_ms is not None]
            inspected = {row[0].id: self.inspect(row[0], data) for row in rows}
            flagged = {run_id for run_id, value in inspected.items() if value[4]}
            selected_tokens = sum(s.tokens_selected for s in records)
            cached = [s for s in records if s.cache_hit_tokens is not None]
            cached_base = sum(s.tokens_after_compaction for s in cached)
            langfuse_status, langfuse_url = self.langfuse()
            settings = getattr(self.store, "source_settings", None)
            sources = {}
            for summary in records:
                for source in summary.source_breakdown_json:
                    row = sources.setdefault(
                        source["source"],
                        {"source": source["source"], "candidate_tokens": 0, "selected_tokens": 0},
                    )
                    row["candidate_tokens"] += source["candidate_tokens"]
                    row["selected_tokens"] += source["selected_tokens"]
            compressed = [v for v in views if v.compaction_applied]
            uncompressed = [v for v in views if not v.compaction_applied]
            outcomes = Counter(terminal_outcome(run, calls[run.id]) for run, _ in rows)
            recorder = getattr(self.store, "contextops", None)
            return dto.ContextOverview(
                administrator=self.administrator,
                window=filters.window,
                session_count=len(session_turns),
                run_count=len(views),
                avg_turns=mean(session_turns.values()) if session_turns else None,
                min_turns=min(session_turns.values()) if session_turns else None,
                max_turns=max(session_turns.values()) if session_turns else None,
                avg_search_turns=mean(search_turns(run, steps[run.id]) for run, _ in rows)
                if views
                else None,
                turn_completion_breakdown=counts(Counter(v.status for v in views)),
                terminal_outcome_counts=counts(outcomes),
                omission_rate=sum(s.omitted_count for s in records) / candidate
                if candidate
                else None,
                compaction_rate=len(compressed) / len(views) if views else None,
                context_duration_share=sum(
                    s.duration_ms
                    for v in views
                    if v.duration_ms is not None
                    for s in summaries[v.run_id]
                )
                / sum(durations)
                if durations and sum(durations)
                else None,
                compressed_completion_rate=sum(v.status == "completed" for v in compressed)
                / len(compressed)
                if compressed
                else None,
                uncompressed_completion_rate=sum(v.status == "completed" for v in uncompressed)
                / len(uncompressed)
                if uncompressed
                else None,
                duration=distribution("run", durations),
                token_upper_bound=sum(s.tokens_after_compaction for s in records)
                if records
                else None,
                measured_tokens=sum(v.total_tokens or 0 for v in views)
                if views and all(v.total_tokens is not None for v in views)
                else None,
                failure_count=sum(v.status == "failed" for v in views) if views else None,
                security_blocked_count=sum(
                    e.hit_count for e in security if e.action in {"block", "deny"}
                )
                if views
                else None,
                redacted_count=sum(e.hit_count for e in security if e.action == "redact")
                if views
                else None,
                source_breakdown=list(sources.values()),
                security_distribution=counts(
                    Counter(
                        {
                            kind: sum(e.hit_count for e in security if e.event_type == kind)
                            for kind in {e.event_type for e in security}
                        }
                    )
                ),
                projection_errors=recorder.errors if recorder else 0,
                projection_dropped=recorder.dropped if recorder else 0,
                trend=self.trend(rows, data, flagged, filters.window),
                compaction_saving_rate=max(
                    0.0, 1 - sum(s.tokens_after_compaction for s in records) / selected_tokens
                )
                if selected_tokens
                else None,
                cache_hit_rate=min(1.0, sum(s.cache_hit_tokens or 0 for s in cached) / cached_base)
                if cached_base
                else None,
                anomaly_count=len(flagged) if views else None,
                empty_result_count=sum(value[2] for value in inspected.values()) if views else None,
                short_session_rate=sum(1 <= n <= 3 for n in session_turns.values())
                / len(session_turns)
                if session_turns
                else None,
                security_mode=getattr(settings, "compass_context_security_mode", None),
                langfuse_status=langfuse_status,
                langfuse_url=langfuse_url,
                policy_versions=counts(
                    Counter(digest for _, digest in {(s.run_id, s.policy_digest) for s in records})
                ),
            )

    def intents(self, filters):
        with Session(self.store.engine) as tx:
            rows, data = self.filtered_dataset(tx, filters)
            records = []
            excluded_historical = 0
            for row in rows:
                run = row[0]
                classifiers = [s for s in data[1][run.id] if s.kind == "classifier"]
                if not classifiers:
                    continue
                if not data[0][run.id]:
                    excluded_historical += 1
                    continue
                source = (
                    "fallback"
                    if any("flash" in s.node_key for s in classifiers)
                    and any("jev" in s.node_key for s in classifiers)
                    else "flash"
                    if any("flash" in s.node_key for s in classifiers)
                    else "jev"
                    if any("jev" in s.node_key for s in classifiers)
                    else "unknown"
                )
                view = self.view(row, data)
                scene_id = view.scene_id or "unclassified"
                category_id, category_name, scene_name = scene_meta(scene_id)
                values = [milliseconds(s.created_at, s.finished_at) for s in classifiers]
                records.append(
                    dto.ContextIntentRecord(
                        run_id=run.id,
                        scene_id=scene_id,
                        scene_name=scene_name,
                        category_id=category_id,
                        category_name=category_name,
                        source=source,
                        duration_ms=sum(v or 0 for v in values)
                        if all(v is not None for v in values)
                        else None,
                        created_at=run.created_at,
                        semantic_version=next(
                            (s.semantic_version for s in data[0][run.id] if s.semantic_version),
                            None,
                        ),
                    )
                )
            labels = sorted({r.scene_id for r in records})
            split = defaultdict(Counter)
            for record in records:
                split[record.scene_id][record.source] += 1
            return dto.ContextIntents(
                distribution=counts(Counter(r.scene_id for r in records)),
                sources=counts(Counter(r.source for r in records)),
                durations=[
                    distribution(
                        scene_meta(scene)[2] or scene,
                        [r.duration_ms for r in records if r.scene_id == scene],
                        scene_id=scene,
                    )
                    for scene in labels
                ],
                recent=records[:100],
                breakdown=[
                    dto.ContextIntentBreakdown(
                        scene_id=item.name,
                        name=scene_meta(item.name)[2] or item.name,
                        category_id=scene_meta(item.name)[0],
                        category_name=scene_meta(item.name)[1],
                        count=item.count,
                        jev=split[item.name]["jev"],
                        flash=split[item.name]["flash"],
                        fallback=split[item.name]["fallback"],
                        unknown=split[item.name]["unknown"],
                    )
                    for item in counts(Counter(r.scene_id for r in records))
                ],
                excluded_historical_count=excluded_historical,
            )

    def anomalies(self, filters):
        with Session(self.store.engine) as tx:
            rows, data = self.filtered_dataset(tx, filters)
            failed, stages, bad, reasons = Counter(), defaultdict(list), [], []
            empty = truncated = 0
            for row in rows:
                run = row[0]
                nodes, failures, run_empty, run_truncated, codes, stage = self.inspect(run, data)
                for node in nodes:
                    stages[node.phase].append(node.duration_ms)
                for node in failures:
                    failed[node.label] += 1
                empty += run_empty
                truncated += run_truncated
                if codes:
                    bad.append(self.view(row, data))
                    reasons.append(
                        dto.ContextAnomalyReason(
                            run_id=run.id,
                            reasons=codes,
                            failed_labels=sorted({n.label for n in failures}),
                            phase=stage,
                        )
                    )
            return dto.ContextAnomalies(
                failures=counts(failed),
                empty_retrieval_count=empty,
                truncation_count=truncated,
                phase_durations=[
                    distribution(p, stages[p])
                    for p in ("identify", "research", "context", "answer")
                ],
                runs=bad[:100],
                reasons=reasons[:100],
            )

    def security_events(self, filters, event_type=None, action=None):
        if not self.administrator:
            raise ModelError("forbidden", 403)
        with Session(self.store.engine) as tx:
            run_ids = self.query(filters).with_only_columns(AgentRun.id).order_by(None)
            query = (
                select(ContextSecurityEvent)
                .where(
                    *self.scope(ContextSecurityEvent),
                    ContextSecurityEvent.created_at
                    >= datetime.now(UTC)
                    - timedelta(days={"24h": 1, "7d": 7, "30d": 30}[filters.window]),
                    or_(
                        ContextSecurityEvent.run_id.in_(run_ids),
                        ContextSecurityEvent.run_id.is_(None),
                    ),
                )
                .order_by(ContextSecurityEvent.created_at.desc(), ContextSecurityEvent.id.desc())
            )
            if filters.actor_id:
                query = query.where(ContextSecurityEvent.actor_id == filters.actor_id)
            if filters.cursor:
                previous = tx.scalar(query.where(ContextSecurityEvent.id == filters.cursor))
                if previous is None:
                    raise ModelError("cursor_invalid", 422)
                query = query.where(
                    or_(
                        ContextSecurityEvent.created_at < previous.created_at,
                        and_(
                            ContextSecurityEvent.created_at == previous.created_at,
                            ContextSecurityEvent.id < previous.id,
                        ),
                    )
                )
            if event_type:
                query = query.where(ContextSecurityEvent.event_type == event_type)
            if action:
                query = query.where(ContextSecurityEvent.action == action)
            items = list(tx.scalars(query.limit(filters.limit + 1)))
            page = items[: filters.limit]
            return dto.ContextSecurityEvents(
                items=[
                    dto.ContextSecurityEventView(
                        **{
                            key: getattr(e, key)
                            for key in dto.ContextSecurityEventView.model_fields
                        }
                    )
                    for e in page
                ],
                next_cursor=page[-1].id if len(items) > len(page) else None,
            )
