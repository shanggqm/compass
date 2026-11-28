"""Bind task completion and tools to the generic Harness loop."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import TYPE_CHECKING

from compass_agent.harness.contracts import RepeatStep, RunFailure
from compass_agent.harness.executor import AgentExecutor
from compass_agent.runtime.context import (
    AgentDefinition,
    RunRoute,
    generate_input,
)
from compass_agent.runtime.session import RuntimeSession
from compass_agent.runtime.task_state import TaskState
from compass_agent.schemas import (
    RUN_STREAM_LIMITS,
    AnalysisFileNone,
    ChatMessage,
    RunCompleted,
    RunProgressEvent,
    RunResult,
    TrustedContext,
)
from compass_agent.tools.contracts import (
    ToolControl,
)

if TYPE_CHECKING:
    from compass_agent.workflows.assets import WorkflowAssets
    from compass_agent.workflows.drafts import WorkflowDrafts


EmitProgress = Callable[[RunProgressEvent], None]


class RuntimeTurn(AgentExecutor):
    definition: AgentDefinition
    workflow_assets: WorkflowAssets | None = None
    workflow_drafts: WorkflowDrafts | None = None

    async def execute_turn(
        self,
        state: TaskState,
        route: RunRoute,
        context: TrustedContext,
        control: ToolControl,
        controlled: RuntimeSession | None,
        emit: EmitProgress,
        *,
        publish_text: bool = True,
        max_steps: int | None = None,
    ) -> RunResult:
        from compass_agent.execution_context import current_execution, execution_path

        journal = current_execution.get()
        if (getattr(journal, "context_binding", None) or {}).get("answerProtocol") == 3:
            from compass_agent.runtime.stream_answer_turn import execute_stream_answer_turn

            return await execute_stream_answer_turn(
                self, state, route, context, control, controlled, emit, max_steps=max_steps
            )
        if (getattr(journal, "context_binding", None) or {}).get("answerProtocol") == 2:
            from compass_agent.runtime.answer_turn import execute_answer_turn

            return await execute_answer_turn(
                self, state, route, context, control, controlled, emit, max_steps=max_steps
            )
        managed = bool(getattr(journal, "context_binding", None)) and route.deliverable in (
            "business_qa",
            "data_analysis",
            "dashboard",
            "business_report",
        )
        if (getattr(journal, "context_binding", None) or {}).get("search") and not managed:
            return RunCompleted(
                status="completed",
                finalAnswer="请补充要核对的经营问题、对象或交付目标。",
                file=AnalysisFileNone(status="none"),
            )
        search = getattr(state.tools, "search", None) if managed else None
        structured_delivery = (
            managed
            and route.deliverable != "business_qa"
            and "/subagent:" not in execution_path.get()
        )
        if controlled and journal:
            journal.state_providers[execution_path.get()] = controlled.checkpoint_state
        budget = controlled.budget if controlled else None
        repaired = False
        next_phase = None
        original_messages = list(state.messages)
        question = next(
            (m.content or "" for m in reversed(original_messages) if m.role == "user"), ""
        )
        if (
            getattr(journal, "context_binding", None)
            and journal.context_binding.get("registry")
            and "/subagent:" not in execution_path.get()
        ):
            from compass_agent.semantic.clarification import continue_selected_result
            from compass_agent.semantic.service import RegistryTools

            registry_tools = RegistryTools(journal, context)
            if journal.approved_proposals and (
                continuation := await continue_selected_result(registry_tools)
            ):
                return continuation

        def begin_phase(phase, correction=None, analysis=None):
            nonlocal next_phase
            next_phase = phase
            if search:
                from compass_agent.context_runtime.search_control import search_messages

                state.messages = search_messages(search, state, question, phase, budget, correction)
                state.continuation_token = None
                return
            facts = [
                {
                    "tool": call.function.name,
                    "callId": call.id,
                    "status": result.status,
                    "observation": result.observation_json,
                }
                for previous in state.steps
                for call, result in zip(previous.tool_calls, previous.tool_results, strict=False)
            ]
            material = {"question": question, "observations": facts}
            if phase == "generate" and structured_delivery:
                from compass_agent.context_runtime.delivery import accepted_views

                material["acceptedViews"] = accepted_views(state)
            if analysis:
                material["analysis"] = analysis
            if correction:
                material["formatCorrection"] = correction
            state.messages = [
                *original_messages[:-1],
                ChatMessage(role="user", content=json.dumps(material, ensure_ascii=False)),
            ]
            state.continuation_token = None

        def prepare():
            if search and next_phase is None:
                begin_phase("plan" if route.requires_plan else "query")
            request = generate_input(
                self.definition,
                state.messages,
                state.continuation_token,
                route,
                controlled=controlled is not None,
                defer_budget=managed,
            )
            if state.tools is not None:
                request = request.model_copy(update={"tools": state.tools.definitions()})
            if managed:
                journal.context_questions[execution_path.get()] = question
                if not state.continuation_token:
                    if next_phase is not None:
                        journal.context_phase = next_phase
                    elif not request.tools:
                        journal.context_phase = "generate"
                        if structured_delivery:
                            begin_phase("generate")
                            request = request.model_copy(update={"messages": state.messages})
                    elif route.requires_plan and any(
                        t.function.name == "todo_write" for t in request.tools
                    ):
                        journal.context_phase = "plan"
                    else:
                        journal.context_phase = "query"
            return request, None

        async def finish_delivery(step, text):
            from compass_agent.context_runtime.delivery import complete_delivery, query_observations

            registry = journal.context_binding.get("registry")
            completed = complete_delivery(
                route.deliverable,
                text,
                state,
                step,
                journal,
                allow_verified_report=repaired and bool(registry),
            )
            if registry:
                from compass_agent.semantic.service import RegistryTools

                await RegistryTools(journal, context).report_references(
                    query_observations(state).values()
                )
            return completed

        async def handle(step, result, prepared):
            nonlocal repaired
            calls = tuple(result.tool_calls)
            if search:
                from compass_agent.context_runtime.search_control import parse_targets

                if journal.context_phase != "generate" and result.text:
                    try:
                        checkpoint = json.loads(result.text)
                    except (TypeError, ValueError):
                        checkpoint = None
                    if isinstance(checkpoint, dict) and set(checkpoint) == {"checkpoint"}:
                        import asyncio

                        from compass_agent.context_runtime.delivery import validation_feedback
                        from compass_agent.context_runtime.search_verification import verify_draft

                        try:
                            await asyncio.to_thread(
                                verify_draft,
                                journal,
                                step,
                                json.dumps(checkpoint["checkpoint"], ensure_ascii=False),
                                context,
                                route.deliverable,
                                checkpoint=True,
                                scene=route.scene_id,
                            )
                        except ValueError as error:
                            search.claim_feedback = {"correction": validation_feedback(error)}
                        if not calls:
                            begin_phase("generate", "请交付全题已核结果及待补事项。")
                            return RepeatStep()
                targets = (
                    parse_targets(result.text) if journal.context_phase != "generate" else None
                )
                if targets:
                    previous_targets = dict(search.targets)
                    accepted = search.plan(targets)
                    if not calls:
                        search.plan_only_repeats = (
                            search.plan_only_repeats + 1
                            if previous_targets == search.targets
                            else 0
                        )
                        if search.plan_only_repeats >= 2:
                            search.stop_reason = "no_progress"
                            begin_phase("generate", "重复规划未取得新材料，请交付已核部分及缺口。")
                        else:
                            begin_phase(
                                "query",
                                "请执行待核事项的下一项查询。"
                                if accepted
                                else "请保留已登记的必需事项，按未决条件查询。",
                            )
                        return RepeatStep()
                if calls:
                    search.plan_only_repeats = 0
                if len(calls) > RUN_STREAM_LIMITS["toolsPerStep"]:
                    begin_phase(
                        "query",
                        f"每批上限为 {RUN_STREAM_LIMITS['toolsPerStep']} 个调用，请缩小本批查询。",
                    )
                    return RepeatStep()
            if state.trace:
                state.trace.bind_calls(str(step.id), calls, ())
            if not calls:
                control.require_running()
                if search:
                    import asyncio

                    from compass_agent.context_runtime.search_verification import (
                        partial_delivery,
                        verify_draft,
                    )

                    comparison = search.pin.get("reportFormat") == "comparison_brief"
                    if comparison and journal.context_phase != "generate":
                        begin_phase("generate")
                        return RepeatStep()
                    try:
                        candidate = await asyncio.to_thread(
                            verify_draft,
                            journal,
                            step,
                            result.text,
                            context,
                            route.deliverable,
                            scene=route.scene_id,
                        )
                    except ValueError as error:
                        from pydantic import ValidationError

                        from compass_agent.context_runtime.delivery import validation_feedback
                        from compass_agent.context_runtime.search_claims import SearchGap

                        issue = (
                            "请修正草稿字段："
                            + json.dumps(validation_feedback(error), ensure_ascii=False)
                            if isinstance(error, ValidationError)
                            else str(error)[:300]
                        )
                        repair_query = not search.stop_reason and (
                            isinstance(error, SearchGap) or not search.batches
                        )
                        marker = (issue, len(search.seen))
                        format_issue = isinstance(error, ValidationError) or isinstance(
                            error.__cause__, json.JSONDecodeError
                        )
                        if (
                            format_issue
                            and search.format_repaired
                            or (
                                journal.context_phase == "generate"
                                and not repair_query
                                and (
                                    marker in search.repair_attempts
                                    or len(search.repair_attempts) >= 4
                                )
                            )
                        ):
                            search.stop_reason = search.stop_reason or "blocked"
                            return await asyncio.to_thread(
                                partial_delivery, journal, context, step, route.deliverable
                            )
                        if repair_query:
                            if getattr(search, "last_gap", None) == marker:
                                search.stop_reason = "no_progress"
                            search.last_gap = marker
                        else:
                            search.repair_attempts.add(marker)
                        if format_issue:
                            search.format_repaired = True
                        repaired = journal.context_phase == "generate" and not repair_query
                        begin_phase(
                            "query" if repair_query and not search.stop_reason else "generate",
                            issue,
                        )
                        return RepeatStep()
                    if (candidate._citations or {}).get("decisionComplete"):
                        search.stop_reason = "sufficient"
                    else:
                        search.stop_reason = search.stop_reason or "blocked"
                    return candidate
                if managed and journal.context_phase == "plan":
                    begin_phase("query")
                    return RepeatStep()
                if managed and route.deliverable != "business_qa":
                    if journal.context_phase != "generate":
                        observed = any(
                            call.function.name in {"database_query", "query_metric"}
                            and observation.status == "ok"
                            for prior in state.steps
                            for call, observation in zip(
                                prior.tool_calls, prior.tool_results, strict=False
                            )
                        )
                        if (
                            route.deliverable == "data_analysis"
                            and observed
                            and journal.context_phase == "query"
                        ):
                            begin_phase("analyze")
                        else:
                            begin_phase(
                                "generate",
                                analysis=result.text
                                if journal.context_phase == "analyze"
                                else None,
                            )
                        return RepeatStep()
                    if route.deliverable == "dashboard" and not any(
                        call.function.name in {"database_query", "query_metric"}
                        and observation.status == "ok"
                        for prior in state.steps
                        for call, observation in zip(
                            prior.tool_calls, prior.tool_results, strict=False
                        )
                    ):
                        return RunCompleted(
                            status="completed",
                            finalAnswer="本次没有取得可用于看板的授权数据。请确认数据连接和指标范围后重试。",
                            file=AnalysisFileNone(status="none"),
                        )
                    try:
                        return await finish_delivery(step, result.text)
                    except ValueError as error:
                        if repaired:
                            raise RunFailure("model_incomplete") from None
                        repaired = True
                        from compass_agent.context_runtime.delivery import validation_feedback
                        from compass_agent.prompt_assets import prompt_text

                        begin_phase(
                            "generate",
                            {
                                "instruction": prompt_text("support/delivery/repair.md"),
                                "validation": validation_feedback(error),
                            },
                        )
                        return RepeatStep()
                answer = self._final_answer(result.text)
                if managed and journal.context_binding.get("registry"):
                    from compass_agent.context_runtime.delivery import query_observations
                    from compass_agent.semantic.receipts import with_scope

                    answer = with_scope(
                        answer,
                        query_observations(state),
                        RUN_STREAM_LIMITS["answerBytes"],
                    )
                candidate = RunCompleted(
                    status="completed",
                    finalAnswer=answer,
                    file=AnalysisFileNone(status="none"),
                )
                return candidate
            if len(calls) > RUN_STREAM_LIMITS["toolsPerStep"]:
                raise RunFailure("model_incomplete")
            if not controlled and max_steps is not None and step.index >= max_steps:
                raise RunFailure("step_limit")
            return None

        async def dispatch(step, request):
            if state.tools is None:
                raise RunFailure("internal_error")
            waiting = await state.tools.dispatch(step, request.tools)
            # The provider's native continuation accepts tool observations only. Start a
            # fresh frame when the owner has spoken, so their answer has the user role.
            if (
                managed
                and not search
                and not waiting
                and any(
                    choice.get("kind") == "semantic_choice" and choice.get("stepId") == str(step.id)
                    for choice in journal.approved_proposals
                )
            ):
                begin_phase("query")
            if search and not waiting:
                from compass_agent.context_runtime.search_control import delivery_reserve

                async def decide():
                    assert search is not None
                    seconds = max(0, int(control.remaining))
                    reason = search.stop_reason
                    if not reason and delivery_reserve(budget, route, seconds):
                        reason = "budget_exhausted"
                    return {"seconds": seconds, "stopReason": reason}

                decision = await journal.local_read(
                    f"{execution_path.get()}/verify:{step.id}",
                    {"calls": [call.id for call in step.tool_calls]},
                    decide,
                    kind="control",
                    # Checkpoint every batch. Rebuild a long search from its journal
                    # once per four batches while this worker still owns the lease.
                    yield_boundary=step.index % 4 == 0 or bool(search.stop_reason),
                )
                search.stop_reason = decision["stopReason"]
                search.feedback["remainingSeconds"] = decision["seconds"]
                search.feedback["stopReason"] = search.stop_reason or "continue"
                if search.stop_reason:
                    begin_phase("generate")
                elif len(search.targets) > 1 and any(
                    item["kind"] in {"metric", "detail"}
                    for item in search.feedback.get("newFacts", [])
                ):
                    begin_phase("analyze")
                else:
                    begin_phase("query")
            return waiting

        async def handle_incomplete(step, result):
            nonlocal repaired
            if result.error_code == "empty_output":
                if search and search.evidence and not search.format_repaired:
                    search.format_repaired = True
                    begin_phase("generate", "请用本轮已打开的原文和查询结果交付分析。")
                    return RepeatStep()
                return None
            if (
                search
                and not repaired
                and not search.format_repaired
                and (
                    journal.context_phase == "generate"
                    or result.error_code in {"response_too_large", "output_truncated"}
                )
            ):
                repaired = True
                search.format_repaired = True
                begin_phase(
                    "generate",
                    "请用 comparisons 引用两期查询行，并在 notes 中提交判断及行动。"
                    if search.pin.get("reportFormat") == "comparison_brief"
                    else "请用 series 的 rows / columns 提交表格。已有草稿只需提交改动项。",
                )
                return RepeatStep()
            if search:
                return None
            if (
                not structured_delivery
                or journal.context_phase != "generate"
                or result.error_code
                not in {"invalid_json", "schema_validation_failed", "output_truncated"}
            ):
                return None
            if repaired:
                if route.deliverable in {
                    "business_report",
                    "data_analysis",
                } and journal.context_binding.get("registry"):
                    try:
                        return await finish_delivery(step, None)
                    except ValueError:
                        return None
                return None
            from compass_agent.prompt_assets import prompt_text

            repaired = True
            begin_phase("generate", prompt_text("support/delivery/repair.md"))
            return RepeatStep()

        return await self.execute_tools(
            state,
            route,
            context,
            control,
            emit,
            prepare,
            handle,
            dispatch,
            budget=budget,
            publish_text=publish_text,
            model_timeout=None if search else 25,
            public_response=lambda response: not bool(search),
            handle_incomplete=handle_incomplete,
        )
