"""Protocol 3 work and streaming delivery in the existing supervised AgentLoop."""

import asyncio
from dataclasses import replace
from hashlib import sha256
from uuid import uuid5

from compass_agent.context_runtime.answer_verification import check_access
from compass_agent.context_runtime.source_access import read_private, save_private
from compass_agent.context_runtime.stream_answer import (
    citations_for,
    evidence_prompt,
    ordered_evidence,
    stream_artifact,
)
from compass_agent.execution_context import current_execution, execution_path
from compass_agent.harness.contracts import RepeatStep, RunFailure
from compass_agent.runtime.answer_turn import needs_delivery_reserve
from compass_agent.runtime.context import generate_input
from compass_agent.runtime.private_state import digest
from compass_agent.schemas import RUN_STREAM_LIMITS, AnalysisFileNone, ChatMessage, RunCompleted
from compass_agent.tools.assembler import DIRECT_STREAM_SCENES


async def execute_stream_answer_turn(
    executor, state, route, trusted, control, controlled, emit, *, max_steps=None
):
    journal = current_execution.get()
    assert journal is not None
    if route.deliverable not in {"business_qa", "data_analysis", "dashboard", "business_report"}:
        route = replace(route, deliverable="business_qa")
    path = execution_path.get()
    budget = controlled.budget if controlled else None
    if controlled:
        journal.state_providers[path] = controlled.checkpoint_state
    question = journal.user_question
    journal.context_questions[path] = question
    from compass_agent.context_runtime.evidence import user_input

    await asyncio.to_thread(user_input, journal, question)
    turns = getattr(journal, "answer_turn", {})
    turn = turns.setdefault(path, {"purpose": "work"})
    journal.answer_turn = turns
    definitions = state.tools.definitions()
    entries = state.tools.runtime.registry.entries
    selection = journal.tool_selection
    streaming = not route.tools_enabled or (
        route.scene_id in DIRECT_STREAM_SCENES
        and not any(selection.values())
        and not getattr(route, "required_tools", ())
        and not journal.goal_id
    )
    mode, artifact_format, ordered = "plain", None, []
    delivery_only = False
    forced_delivery = False
    completion = "complete"
    review_pending = journal.context_binding.get("completionReview") == 1
    limit = min(
        max_steps or RUN_STREAM_LIMITS["steps"],
        budget.limits.steps if budget else RUN_STREAM_LIMITS["steps"],
    )

    def evidence():
        return getattr(journal, "answer_evidence", {}).get(path, {})

    def review_prompt():
        from compass_agent.context_runtime.pipeline import exact

        with journal.store.transaction() as tx:
            return exact(tx, journal.context_binding, ["answer/review.md"])[0].body

    async def prepare():
        nonlocal streaming, mode, ordered, delivery_only, forced_delivery

        remaining = control.remaining

        def transition():
            # Wall-clock reserve decisions must replay across lease owners unchanged.
            key = f"answer/v3/phase/{path}/{state.steps[-1].index}"
            with journal.store.transaction() as tx:
                _, run = journal.store.lock(tx, journal.run_id)
                journal.store.require_epoch(run, journal.epoch)
                saved = read_private(journal.store, tx, run.id, key)
                if saved is not None:
                    return saved["transition"]
                completed_steps = len(state.steps) - 1
                reserve = (
                    completed_steps >= limit - 2
                    or (budget and budget.tools >= budget.limits.tools)
                    or needs_delivery_reserve(budget, route, 4096)
                    or remaining < 50
                )
                forced = completed_steps >= limit - 1 or remaining < 25 or delivery_only
                decision = (
                    "answer"
                    if streaming
                    else "forced"
                    if forced
                    else "delivery"
                    if reserve
                    else "work"
                )
                save_private(journal.store, tx, run, key, {"transition": decision}, [])
                return decision

        decision = await asyncio.to_thread(transition)
        if decision == "forced":
            forced_delivery = True
            streaming = True
            mode = (
                "evidence"
                if any(item["kind"] != "user_input" for item in evidence().values())
                else "plain"
            )
            state.messages.append(
                ChatMessage(
                    role="user", content="请交付已完成的结果，并明确尚未解决的事项和下一步。"
                )
            )
        elif decision == "delivery":
            delivery_only = True
            forced_delivery = True
            state.continuation_token = None
            state.messages.append(
                ChatMessage(
                    role="user",
                    content="请单独调用 final_answer，交付已有结果并说明尚未解决的事项。",
                )
            )
        turn["purpose"] = "answer_stream" if streaming else "work"
        turn["schema"] = None
        turn["delivery_only"] = delivery_only and not streaming
        if streaming:
            if not await asyncio.to_thread(check_access, journal, trusted):
                raise RunFailure("internal_error", reason="answer_source_access_changed")
            ordered = ordered_evidence(evidence())
            turn.update(mode=mode, ordered_evidences=evidence_prompt(ordered))
            state.continuation_token = None
            journal.context_phase = "generate"
        else:
            journal.context_phase = "query"
        request = generate_input(
            executor.definition,
            state.messages,
            state.continuation_token,
            route,
            controlled=controlled is not None,
            defer_budget=True,
        )
        from compass_agent.context_runtime.model_step import model_limits

        limits = model_limits(route, budget)
        output_caps = (
            [
                p.output_tokens
                for p in budget.profiles
                if (p.provider_id, p.binding_version) == (route.provider_id, route.binding_version)
            ]
            if budget
            else []
        )
        selected = (
            [d for d in definitions if entries[d.function.name].completion]
            if delivery_only
            else definitions
        )
        return request.model_copy(
            update={
                "tools": [] if streaming else selected,
                "stream": route.stream if streaming else False,
                "thinking": False if streaming else request.thinking,
                "reasoning_effort": None if streaming else request.reasoning_effort,
                "max_tokens": min(
                    8192, limits.max_output_tokens or request.max_tokens, *output_caps
                ),
            }
        ), None

    async def accept(step, response, prepared):
        if not streaming:
            if response.tool_calls:
                if len(response.tool_calls) > RUN_STREAM_LIMITS["toolsPerStep"]:
                    state.messages.append(
                        ChatMessage(
                            role="user",
                            content=(
                                f"单轮最多调用 {RUN_STREAM_LIMITS['toolsPerStep']} 个工具，"
                                "本批尚未执行，请拆分调用。"
                            ),
                        )
                    )
                    state.continuation_token = None
                    return RepeatStep()
                if state.trace:
                    state.trace.bind_calls(str(step.id), tuple(response.tool_calls), entries)
                return None
            state.messages.append(ChatMessage(role="assistant", content=response.text))
            state.messages.append(
                ChatMessage(
                    role="user",
                    content="请继续完成所需工具调用，或单独调用 final_answer 开始最终回答。",
                )
            )
            state.continuation_token = None
            return RepeatStep()
        if response.tool_calls or not response.text.strip():
            raise RunFailure("model_incomplete")
        if not await asyncio.to_thread(check_access, journal, trusted):
            raise RunFailure("internal_error", reason="answer_source_access_changed")
        body = executor._final_answer(response.text)
        citations = (
            citations_for(journal.run_id, body, ordered)
            if mode == "evidence"
            else {"anchors": [], "details": {}}
        )
        artifact = stream_artifact(
            artifact_format,
            evidence(),
            body,
            f"{path}/model:{step.id}",
            journal.context_date.isoformat(),
        )
        identifier = str(uuid5(journal.run_id, f"answer-v3:{path}:{step.id}"))
        acceptance = {
            "candidateId": identifier,
            "candidateDigest": digest({"mode": mode, "artifactFormat": artifact_format}),
            "evidenceRevision": digest(evidence()),
            "textDigest": digest(body),
            "citationsDigest": digest(citations),
            "mode": mode,
            "completion": "partial" if forced_delivery else completion,
            "executionPath": path,
            "artifactDigest": sha256(artifact.data).hexdigest() if artifact else None,
        }

        def save():
            with journal.store.transaction() as tx:
                _, run = journal.store.lock(tx, journal.run_id)
                journal.store.require_epoch(run, journal.epoch)
                save_private(
                    journal.store, tx, run, f"answer/v3/accepted/{identifier}", acceptance, []
                )

        await asyncio.to_thread(save)
        completed = RunCompleted(
            status="completed", finalAnswer=body, file=AnalysisFileNone(status="none")
        )
        completed._citations, completed._answer_acceptance, completed._delivery = (
            citations,
            acceptance,
            artifact,
        )
        return completed

    async def dispatch(step, request):
        nonlocal streaming, mode, artifact_format, review_pending, completion
        waiting = await state.tools.dispatch(step, request.tools)
        if waiting:
            return waiting
        for call, result in zip(step.tool_calls, state.tools.last_results, strict=True):
            if call.function.name == "final_answer" and result.status == "ok":
                if review_pending and not delivery_only:
                    # This bounded work turn is reconstructed from accepted tool results
                    # on replay; it cannot reopen an already streaming answer.
                    state.messages.append(
                        ChatMessage(role="user", content=await asyncio.to_thread(review_prompt))
                    )
                    state.continuation_token = None
                    review_pending = False
                    continue
                mode = result.payload["mode"]
                artifact_format = result.payload.get("artifactFormat")
                completion = result.payload.get("completion", "complete")
                streaming = True
        return None

    async def incomplete(step, response):
        if not streaming and (
            response.error_code == "output_truncated" or response.finish_reason == "length"
        ):
            state.messages.append(
                ChatMessage(
                    role="user",
                    content="本轮工具参数输出被截断，本批尚未执行。请缩短参数或拆分调用后重试。",
                )
            )
            state.continuation_token = None
            return RepeatStep()
        return None

    return await executor.execute_tools(
        state,
        route,
        trusted,
        control,
        emit,
        prepare,
        accept,
        dispatch,
        budget=budget,
        publish_text=lambda: streaming,
        model_timeout=None,
        public_response=lambda response: streaming,
        handle_incomplete=incomplete,
    )
