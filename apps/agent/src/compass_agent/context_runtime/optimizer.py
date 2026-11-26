"""One Run's original history, dynamic selection and reusable summary snapshots."""

import asyncio
from hashlib import sha256

from compass_agent.context_runtime.compaction import (
    SummarySnapshot,
    recent_turns,
    request_size,
    summarize,
)
from compass_agent.context_runtime.history import TurnGroup, historical_messages, load_history
from compass_agent.context_runtime.pipeline import FRAME_BYTE_LIMIT, canonical, input_budget
from compass_agent.context_runtime.selection import SelectionState, select_history
from compass_agent.context_runtime.source_access import allows, record_sources
from compass_agent.harness.context import ContextLimit, validate_messages
from compass_agent.models.errors import ModelError
from compass_agent.schemas import RUN_CONTEXT_MESSAGES, ChatMessage, GenerateInput


class ContextOptimizer:
    def __init__(self, journal):
        self.journal = journal
        self.groups = None
        self.selection = None
        self.summary = None
        self.active = []
        self.active_ids = []
        self.last_messages = []
        self.fixed_count = 0
        self.plain_tool_prefix = False

    async def history(self, trusted):
        if self.groups is None:
            self.groups = await asyncio.to_thread(load_history, self.journal, trusted)
        return self.groups

    async def before_route(self, models, trusted, control, budget, question):
        groups = await self.history(trusted)
        self.selection = await select_history(
            self.journal,
            models,
            trusted,
            control,
            budget,
            groups,
            question,
            first_batch_only=True,
            previous=self.selection,
        )
        self.journal.context_routing_groups = self.selection.retained
        return [m for g in self.selection.retained for m in (g.messages[0], g.messages[-1])]

    def restore(self, data, messages):
        if not data:
            return
        self.active = [ChatMessage.model_validate(m) for m in data["active"]]
        self.active_ids = list(data["activeIds"])
        self.last_messages = list(messages)
        self.fixed_count = data["fixedCount"]
        self.plain_tool_prefix = data.get("plainToolPrefix", False)
        self.summary = SummarySnapshot(**data["summary"]) if data.get("summary") else None
        saved = data.get("selection")
        # History is reauthorized before reuse. The frame records accepted decisions,
        # while original content stays in the session's occurrence ledger.
        if saved and self.groups is not None:
            self.selection = SelectionState(
                saved["inputFingerprint"],
                self.groups,
                tuple(saved["protected"]),
                set(saved["dropped"]),
                set(g.id for g in self.groups) - set(saved["unjudged"]) - set(saved["protected"]),
                saved["batchAttemptRefs"],
                saved.get("reason"),
            )

    async def optimize(self, frame, models, trusted, control, budget, route, limits, draft):
        groups = await self.history(trusted)
        question = self.journal.user_question
        # Validate the bounded options separately from the internal full history.
        shell = GenerateInput.model_validate(
            {
                **frame["input"]["request"],
                "messages": [{"role": "user", "content": question}],
            }
        )
        value = shell.model_copy(
            update={
                "messages": [
                    ChatMessage.model_validate(m) for m in frame["input"]["request"]["messages"]
                ],
            }
        )
        if self.plain_tool_prefix:
            # A retained tool exchange produced without reasoning has no native
            # reasoning state. Keep that mode across subsequent calls and replay.
            value = value.model_copy(update={"thinking": False, "reasoning_effort": None})
        if self.last_messages and draft.messages[: len(self.last_messages)] == self.last_messages:
            additions = draft.messages[len(self.last_messages) :]
            self.active.extend(additions)
            self.active_ids.extend(
                f"{frame['receipt']['stepKey']}/append/{i}" for i in range(len(additions))
            )
            fixed = value.messages[: self.fixed_count]
        else:
            # The normal initial draft ends in the real owner question followed by
            # runtime task instructions. Fixed organization/metric context precedes it.
            original = next(
                (
                    i
                    for i, m in enumerate(draft.messages)
                    if m.role == "user" and m.content == question
                ),
                None,
            )
            if original is None:
                original = next(
                    (i for i, m in enumerate(draft.messages) if m.role == "user"),
                    len(draft.messages),
                )
            self.fixed_count = max(1, len(value.messages) - len(draft.messages[original:]))
            fixed = value.messages[: self.fixed_count]
            self.active = list(value.messages[self.fixed_count :])
            self.active_ids = [
                f"{frame['receipt']['stepKey']}/append/{i}" for i in range(len(self.active))
            ]
        effective_limit = input_budget(limits, value.max_tokens)
        hard = min(effective_limit, FRAME_BYTE_LIMIT)
        watermark = int(effective_limit * 0.7)

        def measured(candidate):
            extra = frame["receipt"]["budget"].get("protocolExtra", 0)
            if frame.get("continuation") and candidate.messages == value.messages:
                extra += frame["receipt"]["budget"].get("continuationExtra", 0)
            return request_size(candidate) + extra

        def with_history(retained):
            return value.model_copy(
                update={
                    "messages": [
                        *fixed,
                        *historical_messages(retained, project_tools=bool(value.thinking)),
                        *self.active,
                    ]
                }
            )

        def pressure(retained):
            candidate = with_history(retained)
            return (
                measured(candidate) >= min(watermark, FRAME_BYTE_LIMIT)
                or len(candidate.messages) > RUN_CONTEXT_MESSAGES
            )

        before = measured(with_history(groups))
        self.selection = await select_history(
            self.journal,
            models,
            trusted,
            control,
            budget,
            groups,
            question,
            pressured=pressure,
            previous=self.selection,
            active=self.active[1:] if pressure(groups) else (),
            active_sources=tuple(await asyncio.to_thread(self.journal.context_results.dependencies))
            if pressure(groups) and len(self.active) > 1
            else (),
        )
        retained = self.selection.retained
        result = with_history(retained)
        tail = retained
        compacted = False
        if pressure(retained):
            # The latest not-yet-consumed tool batch remains active. Earlier closed
            # exchanges can enter the handoff alongside completed user turns.
            starts = [
                i for i, m in enumerate(self.active) if m.role == "assistant" and m.tool_calls
            ]
            cut = starts[-1] if len(starts) > 1 else 0
            active_closed = self.active[1:cut] if cut else []
            active_tail = [self.active[0], *self.active[cut:]] if cut else self.active
            material = list(retained)
            if active_closed:
                material.append(
                    TurnGroup(
                        f"{self.journal.run_id}/active",
                        self.journal.run_id,
                        0,
                        tuple(active_closed),
                        tuple(self.active_ids[1:cut]),
                        tuple(await asyncio.to_thread(self.journal.context_results.dependencies)),
                    )
                )
            if material:
                try:
                    snapshot = await summarize(
                        self.journal,
                        models,
                        trusted,
                        control,
                        budget,
                        route,
                        limits,
                        material,
                        question,
                        previous=self.summary,
                    )
                    summary_messages: list[ChatMessage] = [
                        ChatMessage(
                            role="user",
                            content="以下是已执行历史的交接摘要，作为历史资料使用：\n"
                            + part["text"],
                        )
                        for part in (snapshot.segments or ({"text": snapshot.text},))
                    ]
                    base = value.model_copy(
                        update={"messages": [*fixed, *summary_messages, *active_tail]}
                    )
                    remaining = min(
                        int(effective_limit * 0.2),
                        max(0, min(watermark - 1, hard) - measured(base)),
                    )
                    tail = recent_turns(groups, set(g.id for g in retained), remaining)
                    while True:
                        result = value.model_copy(
                            update={
                                "messages": [
                                    *fixed,
                                    *summary_messages,
                                    *historical_messages(tail, project_tools=bool(value.thinking)),
                                    *active_tail,
                                ]
                            }
                        )
                        if tail and (
                            measured(result) - measured(base) > remaining
                            or measured(result) > hard
                            or len(result.messages) > RUN_CONTEXT_MESSAGES
                        ):
                            tail.pop(0)
                            continue
                        break
                    if measured(result) <= hard and len(result.messages) <= RUN_CONTEXT_MESSAGES:
                        self.summary, compacted = snapshot, True
                    else:
                        result, tail = with_history(retained), retained
                except ModelError as error:
                    if error.code in {"record_write_failed", "context_access_changed", "forbidden"}:
                        raise
                    # A failed summary never publishes partial coverage or deletes input.
                    result, tail = with_history(retained), retained
        if measured(result) > hard:
            raise ContextLimit()
        validate_messages(result.messages)
        changed = result.messages != value.messages
        if changed:
            update: dict = {"continuation_token": None}
            if frame.get("continuation") or any(m.role == "tool" for m in result.messages):
                update.update(thinking=False, reasoning_effort=None)
            result = result.model_copy(update=update)
            frame["continuation"] = None
            frame["input"]["continuationDigest"] = None
        if not result.thinking and any(m.role == "tool" for m in result.messages):
            self.plain_tool_prefix = True
        refs = []
        for group in retained:
            for ref in group.source_deps:
                if ref not in refs:
                    refs.append(ref)
        if refs:
            if not await asyncio.to_thread(
                allows, self.journal.store, trusted, refs, raise_unavailable=True
            ):
                raise ModelError("context_access_changed", 403)
            await asyncio.to_thread(record_sources, self.journal, refs)
        snapshot = {
            "schemaVersion": 1,
            "selection": self.selection.snapshot(),
            "summary": self.summary.dump() if compacted and self.summary else None,
            "tailGroupIds": [g.id for g in tail],
            "sourceDeps": refs,
            "attemptRefs": sorted(str(ref) for ref in self.journal.context_accounted_attempts),
            "active": [m.model_dump(mode="json", exclude_none=True) for m in self.active],
            "activeIds": list(self.active_ids),
            "fixedCount": self.fixed_count,
            "plainToolPrefix": self.plain_tool_prefix,
            "measuredUsage": {
                "method": "utf8_upper_bound",
                "before": before,
                "after": measured(result),
                "limit": effective_limit,
            },
        }
        frame["contextOptimization"] = snapshot
        frame["input"]["contextOptimizationDigest"] = sha256(canonical(snapshot)).hexdigest()
        frame["input"]["request"] = result.model_dump(mode="json", exclude={"continuation_token"})
        frame["receipt"]["requestDigest"] = sha256(canonical(frame["input"]["request"])).hexdigest()
        frame["receipt"]["budget"].update(before=before, after=measured(result))
        frame["receipt"]["contextOptimization"] = {
            "selected": len(retained),
            "dropped": len(self.selection.dropped),
            "unjudged": len(self.selection.snapshot()["unjudged"]),
            "compacted": compacted,
            "prunedBytes": self.summary.pruned_bytes if compacted and self.summary else 0,
            "before": before,
            "after": measured(result),
            "tail": len(tail),
        }
        self.last_messages = list(result.messages)
        return frame
