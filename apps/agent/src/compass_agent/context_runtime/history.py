"""Authorized conversational occurrences, independent of model-prefix snapshots."""

import json
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from compass_agent.context_runtime.pipeline import canonical
from compass_agent.context_runtime.source_access import allows, assistant_message, final_text
from compass_agent.runtime.entities import AgentAttempt, AgentRun, AgentSession, AgentStep
from compass_agent.runtime.private_state import digest
from compass_agent.schemas import ChatMessage


@dataclass(frozen=True)
class TurnGroup:
    id: str
    run_id: UUID
    ordinal: int
    messages: tuple[ChatMessage, ...]
    ranges: tuple[str, ...]
    source_deps: tuple[dict, ...] = ()

    @property
    def content_hash(self):
        return digest(self.view())

    def view(self):
        return {
            "groupId": self.id,
            "messages": [m.model_dump(mode="json", exclude_none=True) for m in self.messages],
        }

    @property
    def bytes(self):
        return len(canonical(self.view()))


def load_history(journal, trusted):
    """Each question re-reads originals; a previous selection never deletes history."""
    groups = []
    store = journal.store
    with Session(store.engine) as tx:
        current = tx.get(AgentRun, journal.run_id)
        assert current is not None
        session = tx.get(AgentSession, current.session_id)
        assert session is not None
        runs = tx.scalars(
            select(AgentRun)
            .where(
                AgentRun.session_id == current.session_id,
                AgentRun.ordinal >= session.context_start_ordinal,
                AgentRun.ordinal < current.ordinal,
                AgentRun.status.in_(("completed", "failed", "canceled")),
            )
            .order_by(AgentRun.ordinal)
        ).all()
        for run in runs:
            answer = assistant_message(tx, run.id)
            refs = tuple((answer.source_refs if answer else None) or [])
            if not allows(store, trusted, refs, raise_unavailable=True):
                # Same-session action status remains useful after source expiry.
                # Omit the original question, arguments and every tool output.
                observations, coordinates = physical_observations(store, tx, run, arguments=False)
                if observations:
                    groups.append(
                        TurnGroup(
                            str(run.id),
                            run.id,
                            run.ordinal,
                            (
                                ChatMessage(
                                    role="user",
                                    content=(
                                        "此前执行的来源已不可读。以下仅为已接受的工具执行状态；"
                                        "需要继续相关事项时先核实，避免重复执行。"
                                    ),
                                ),
                                *observations,
                            ),
                            (f"{run.id}/status-only", *coordinates),
                        )
                    )
                continue
            question = run.config_snapshot["command"]["question"]
            messages = [ChatMessage(role="user", content=question)]
            ranges = [f"{run.id}/user"]
            covered_calls = set()
            for _step, attempt in tx.execute(
                select(AgentStep, AgentAttempt)
                .join(AgentAttempt, AgentStep.accepted_attempt_id == AgentAttempt.id)
                .where(AgentStep.run_id == run.id, AgentStep.kind == "ctx_history")
                .order_by(AgentStep.ordinal)
            ):
                saved = store.private.open(run.id, "attempt-output", attempt.output_content)
                if digest(saved) != attempt.output_sha256:
                    from compass_agent.models.errors import ModelError

                    raise ModelError("checkpoint_corrupt", 409)
                batch = saved["result"]
                if batch["path"] != "root":
                    continue
                messages.extend(ChatMessage.model_validate(m) for m in batch["messages"])
                ranges.extend(batch["occurrences"])
                covered_calls.update(
                    c["id"] for m in batch["messages"] for c in m.get("tool_calls") or []
                )
            extra, coordinates = physical_observations(store, tx, run, covered_calls)
            messages.extend(extra)
            ranges.extend(coordinates)
            text = final_text(store, tx, run.id)
            if (
                not text
                and run.config_snapshot.get("contextRuntime", {}).get("answerProtocol")
                not in {2, 3}
                and answer
                and answer.public_content
            ):
                text = answer.public_content.get("text")
            if text:
                messages.append(ChatMessage(role="assistant", content=text))
                ranges.append(f"{run.id}/final")
            elif run.status != "completed":
                messages.append(
                    ChatMessage(
                        role="assistant",
                        content=f"执行状态：{run.status}。已记录的动作结果见前文。",
                    )
                )
                ranges.append(f"{run.id}/status")
            if len(messages) > 1:
                groups.append(
                    TurnGroup(
                        str(run.id), run.id, run.ordinal, tuple(messages), tuple(ranges), refs
                    )
                )
    return groups


def physical_observations(store, tx, run, covered_calls=(), *, arguments=True):
    messages, ranges = [], []
    for step, attempt in tx.execute(
        select(AgentStep, AgentAttempt)
        .join(AgentAttempt, AgentStep.accepted_attempt_id == AgentAttempt.id)
        .where(AgentStep.run_id == run.id, AgentStep.kind == "tool")
        .order_by(AgentStep.ordinal)
    ):
        if not step.node_key.startswith("root/physical:"):
            continue
        call_id = step.node_key.removeprefix("root/physical:")
        if call_id in covered_calls:
            continue
        inputs = store.private.open(run.id, "attempt-input", attempt.input_content)
        saved = store.private.open(run.id, "attempt-output", attempt.output_content)
        if digest(inputs) != step.input_sha256 or digest(saved) != attempt.output_sha256:
            from compass_agent.models.errors import ModelError

            raise ModelError("checkpoint_corrupt", 409)
        result = saved["result"]
        observation = {
            "tool": inputs["tool"],
            "callId": call_id,
            "status": result["status"],
            "cleanup": result["cleanup"],
            "reason": result.get("reason_code"),
        }
        if arguments:
            observation["args"] = inputs["args"]
        messages.append(
            ChatMessage(
                role="assistant",
                content="历史工具执行记录（其余批次状态未确认）："
                + json.dumps(observation, ensure_ascii=False, separators=(",", ":")),
            )
        )
        ranges.append(f"{run.id}/physical/{step.id}")
    return messages, ranges


def historical_messages(groups, *, project_tools=False):
    """Closed tool exchanges need no private reasoning signature on a fresh prefix."""
    for group in groups:
        for message in group.messages:
            if project_tools and (message.role == "tool" or message.tool_calls):
                yield ChatMessage(
                    role="assistant",
                    content="历史已闭合工具观察："
                    + json.dumps(
                        message.model_dump(mode="json", exclude_none=True),
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                )
            else:
                yield message


async def record_batch(journal, step, messages, path):
    if not journal.context_results or path != "root":
        return
    # Record the completed batch as occurrences even when content repeats elsewhere.
    data = {
        "path": path,
        "stepId": str(step.id),
        "messages": [m.model_dump(mode="json", exclude_none=True) for m in messages],
        "occurrences": [f"{journal.run_id}/{path}/{step.id}/{i}" for i in range(len(messages))],
    }

    async def save():
        return data

    saved = await journal.local_read(
        f"{path}/history:{step.id}",
        {
            "path": path,
            "stepId": str(step.id),
            "toolCalls": [c.model_dump(mode="json") for c in step.tool_calls],
        },
        save,
        kind="ctx_history",
        yield_boundary=False,
    )
    return [ChatMessage.model_validate(m) for m in saved["messages"]]
