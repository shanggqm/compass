"""Supervise the isolated child loop and execute admitted model/tool requests."""

from __future__ import annotations

import asyncio
import json
import sys
from contextlib import nullcontext
from dataclasses import asdict, dataclass, replace
from uuid import uuid5

from compass_agent.execution_context import (
    current_execution,
    execution_path,
    execution_scope,
    recorded,
)
from compass_agent.goal.contracts import (
    ChildReturn,
    DelegationTask,
    FinalReport,
    ReportToParentInput,
)
from compass_agent.goal.handoff import admit_delegation, delivery, import_inputs, merge_delivery
from compass_agent.harness.autonomy import AutonomyStopped
from compass_agent.harness.contracts import RunFailure, Step, ToolResult
from compass_agent.harness.subagent_process import FRAME_LIMIT
from compass_agent.llm.continuation import canonical
from compass_agent.models.errors import ModelError
from compass_agent.schemas import ExecutionSubAgent, GenerateInput, ToolCall
from compass_agent.tools.batch import dispatch_batch
from compass_agent.tools.contracts import (
    PhaseCanceled,
    ResourceUnconfirmed,
    ToolBatchPolicy,
    ToolExecutionResult,
    ToolRejected,
    json_bytes,
)
from compass_agent.tools.dispatcher import CallControl

CHILD_TOOLS = frozenset({"database_query"})


def child_tools(entries, visible, protocol):
    if protocol != 2:
        return CHILD_TOOLS.intersection(visible)
    return frozenset(
        name
        for name, entry in entries.items()
        if name in visible
        and name not in {"goal", "sub_agent", "todo_write", "create_workflow"}
        and entry.policy.effect == "read_only"
        and entry.policy.approval == "none"
        and not entry.local_effect
        and not entry.completion
        and not getattr(entry.binding, "orchestrates", False)
    )


def _tool_observation(result):
    payload = result["payload"]
    if payload is None:
        payload = {"status": result["status"], "code": result["reason_code"]}
        if result.get("proposal"):
            payload = {**payload, "requiresParentAction": True, "proposal": result["proposal"]}
    return json_bytes(payload).decode()


@dataclass
class ChildSession:
    returned: ChildReturn | None = None


class SubAgent:
    def __init__(self, bindings):
        self.bindings = bindings
        self.semaphore = asyncio.Semaphore(2)

    async def tasks(self, tasks, context, *, group=None):
        runtime = self.bindings.runtime
        assert runtime is not None
        journal = current_execution.get()
        names = child_tools(
            runtime.registry.entries,
            context.visible_names,
            (getattr(journal, "context_binding", None) or {}).get("answerProtocol"),
        )
        budget = self.bindings.budget if self.bindings.controlled else None
        snapshots = await asyncio.to_thread(
            admit_delegation, journal, tasks, context, names, budget
        )

        async def execute(index, task, snapshot):
            async with self.semaphore:
                return await self.run(
                    task.objective, context, group=group, task=task, snapshot=snapshot, index=index
                )

        results = await asyncio.gather(
            *(
                execute(i, t, snapshot)
                for i, (t, snapshot) in enumerate(zip(tasks, snapshots, strict=True))
            )
        )
        return {"deliveries": results}

    async def run(
        self, prompt, context, *, group=None, identifier=None, task=None, snapshot=None, index=None
    ):
        trace = self.bindings.state.trace
        journal = current_execution.get()
        task = task or DelegationTask(name="子任务", objective=prompt)
        runtime = self.bindings.runtime
        assert runtime is not None
        names = child_tools(
            runtime.registry.entries,
            context.visible_names,
            (getattr(journal, "context_binding", None) or {}).get("answerProtocol"),
        )
        if snapshot is None:
            snapshot = (
                await asyncio.to_thread(
                    admit_delegation,
                    journal,
                    [task],
                    context,
                    names,
                    self.bindings.budget if self.bindings.controlled else None,
                )
            )[0]
        parent = None
        prepared = None
        key = f"subagent:{context.step_id}:{context.call_id}" + (
            f":{index}" if index is not None else ""
        )
        child_id = uuid5(context.run_id, "step:" + key)
        child = ChildSession()
        if journal:
            prepared = await journal.subagent_begin(
                key, canonical(snapshot).decode(), context.step_id
            )
            child_id = prepared[0]
            parent = journal.parent.set(child_id)
        try:
            with execution_scope(key):
                info = ExecutionSubAgent(
                    name=task.name, taskName=task.name, childRunId=str(child_id)
                )
                if trace:
                    if identifier is None:
                        identifier = trace.start(
                            "subagent",
                            task.name,
                            subagent=info,
                            **({"parentId": group} if group else {}),
                        )
                    else:
                        trace.update(identifier, title=task.name, subagent=info)
                answer, reason = "", "budget"
                try:
                    parent_budget = self.bindings.budget if self.bindings.controlled else None
                    if parent_budget and not parent_budget.can_slice([task.max_steps]):
                        raise AutonomyStopped("max_steps")
                    with (
                        parent_budget.child_budget(steps=task.max_steps)
                        if parent_budget
                        else nullcontext(context.budget)
                    ) as budget:
                        child_context = replace(
                            context,
                            step_id=child_id,
                            budget=budget,
                            sources={},
                            connection_ids=frozenset(task.connection_ids)
                            if task.connection_ids
                            else context.connection_ids,
                        )
                        if journal:
                            snapshot = await asyncio.to_thread(
                                import_inputs, journal, snapshot, child_id, context.trusted
                            )
                        answer, reason = await self._process(
                            snapshot, task, child_context, identifier, child
                        )
                except AutonomyStopped:
                    reason = "budget"
                except (ToolRejected, RunFailure, ModelError):
                    reason = "error"
                evidence = getattr(journal, "answer_evidence", {}).get(execution_path.get(), {})
                result = delivery(
                    child_id,
                    child.returned,
                    answer,
                    reason,
                    evidence,
                    artifact_ids=[
                        item["id"]
                        for item in snapshot.get("inputs", [])
                        if item["kind"] == "artifact"
                    ],
                )
                modern = (getattr(journal, "context_binding", None) or {}).get(
                    "answerProtocol"
                ) == 2
                if journal:
                    await journal.host_result(prepared, result, notify=False)
                    await asyncio.to_thread(merge_delivery, journal, result, context.trusted)
                if trace and identifier:
                    info = info.model_copy(update={"delivery_status": result["status"]})
                    trace.update(
                        identifier, subagent=info, summary=result["answer"][:512] or "子任务未完成"
                    )
                    trace.finish(
                        identifier, "failed" if result["status"] == "failed" else "completed"
                    )
                return result if modern else {"answer": answer}
        except BaseException as error:
            if trace and identifier:
                trace.finish(
                    identifier,
                    "canceled" if isinstance(error, asyncio.CancelledError) else "failed",
                )
            raise
        finally:
            if journal and parent is not None:
                journal.parent.reset(parent)

    async def _process(self, snapshot, task, context, node_id, child):
        from compass_agent.prompt_assets import prompt_text
        from compass_agent.schemas import RUN_STREAM_LIMITS

        bindings = self.bindings
        runtime = bindings.runtime
        assert runtime is not None
        trace = bindings.state.trace
        journal = current_execution.get()
        names = child_tools(
            runtime.registry.entries,
            context.visible_names,
            (getattr(journal, "context_binding", None) or {}).get("answerProtocol"),
        )
        if task.tools:
            names = names.intersection(task.tools)
        from compass_agent.schemas import ToolDefinition

        modern = (getattr(journal, "context_binding", None) or {}).get("answerProtocol") == 2
        declarations = list(runtime.registry.definitions(names))
        if modern:
            report = json.loads(prompt_text("support/subagent/report-to-parent.json"))
            report["function"]["parameters"] = {
                "type": "object",
                **ReportToParentInput.model_json_schema(by_alias=True),
            }
            declarations.append(ToolDefinition.model_validate(report))
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-I",
            "-m",
            "compass_agent.harness.subagent_process",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            limit=FRAME_LIMIT,
            env={},
        )
        reader, writer = process.stdout, process.stdin
        assert reader is not None and writer is not None
        steps = {}

        async def send(value):
            line = json_bytes(value)
            if len(line) >= FRAME_LIMIT:
                raise ToolRejected("subagent_protocol_error")
            writer.write(line + b"\n")
            await writer.drain()

        try:
            await send(
                {
                    "prompt": canonical(snapshot).decode() if modern else task.objective,
                    "maxSteps": task.max_steps,
                    "system": "\n".join(
                        (
                            prompt_text("support/subagent/analysis.md"),
                            prompt_text("shared/fragments/tool-limit.md").replace(
                                "{{tool_limit}}", str(RUN_STREAM_LIMITS["toolsPerStep"])
                            ),
                            prompt_text("shared/tools/database-query.md"),
                        )
                    ),
                    "tools": [item.model_dump(mode="json") for item in declarations],
                    "controlled": bindings.controlled is not None,
                }
            )
            while True:
                context.require_running()
                line = await reader.readline()
                if not line or len(line) > FRAME_LIMIT:
                    raise ToolRejected("subagent_protocol_error")
                frame = json.loads(line)
                kind = frame["type"]
                if kind == "done" and steps:
                    return bindings.executor._final_answer(frame["answer"]), "natural_end"
                if kind == "error":
                    return "", "step_limit" if frame.get("code") == "step_limit" else "error"
                index = frame["index"]
                if type(index) is not int or not 1 <= index <= task.max_steps:
                    raise ToolRejected("subagent_protocol_error")
                if kind == "model" and index == len(steps) + 1:
                    request = GenerateInput.model_validate(frame["request"])
                    if request.tools != declarations or request.stream or request.thinking:
                        raise ToolRejected("subagent_protocol_error")
                    step, model_node, response = await self._model(index, request, context, node_id)
                    steps[index] = (step, model_node, response.tool_calls)
                    reply = response.model_dump(mode="json")
                    if step.prepared_request is not None:
                        reply = {
                            "response": reply,
                            "messages": [
                                m.model_dump(mode="json") for m in step.prepared_request.messages
                            ],
                        }
                    await send(reply)
                elif kind == "tools" and index == len(steps) and index in steps:
                    step, model_node, calls = steps[index]
                    if not calls or tuple(
                        ToolCall.model_validate(item) for item in frame["calls"]
                    ) != tuple(calls):
                        raise ToolRejected("subagent_protocol_error")
                    steps[index] = (step, model_node, ())
                    observed = await self._tools(
                        step, model_node, tuple(calls), context, names, child, node_id
                    )
                    if child.returned:
                        if trace and model_node:
                            trace.finish(model_node)
                        return child.returned.summary, "returned"
                    await send(observed)
                    if trace and model_node:
                        trace.finish(model_node)
                else:
                    raise ToolRejected("subagent_protocol_error")
        except RunFailure as error:
            raise ToolRejected(error.code) from None
        except ModelError as error:
            raise ToolRejected(bindings.executor._model_error(error.code)) from None
        except (ValueError, OSError, KeyError, TypeError):
            raise ToolRejected("subagent_protocol_error") from None
        except asyncio.CancelledError as error:
            raise PhaseCanceled(
                cleanup_confirmed=context.control.cleanup_confirmed
                and (not isinstance(error, PhaseCanceled) or error.cleanup_confirmed)
            ) from None
        finally:
            writer.close()
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
            await process.wait()

    async def _model(self, index, request, context, node_id):
        bindings = self.bindings
        trace = bindings.state.trace
        identifier = uuid5(context.step_id, f"{context.call_id}:model:{index}")
        step = Step(identifier, index, uuid5(identifier, "attempt"))
        node = (
            trace.start(
                "model",
                "分析子任务",
                parentId=node_id,
                stepId=str(step.id),
                modelCallId=str(step.model_call_id),
            )
            if trace
            else None
        )
        budget = context.budget if bindings.controlled else None
        # The child owns its invocation deadline without replacing the parent Run phase.
        now = asyncio.get_running_loop().time()
        model_control = CallControl(context.control, min(context.control.deadline_at, now + 25))
        try:
            if budget:
                budget.require_step()
                budget.generation = step.model_call_id
            async with asyncio.timeout_at(model_control.deadline_at):
                response = await bindings.executor._invoke_model(
                    step,
                    request,
                    bindings.route,
                    context.trusted,
                    model_control,
                    budget,
                    publish_text=False,
                )
            if budget:
                budget.require_continue()
                if response.error_code == "model_timeout":
                    raise AutonomyStopped("execution_timeout")
            bindings.executor._require_complete(response)
        except TimeoutError:
            if trace and node:
                trace.finish(node, "failed")
            raise ToolRejected("subagent_model_timeout") from None
        except BaseException as error:
            if trace and node:
                trace.finish(
                    node, "canceled" if isinstance(error, asyncio.CancelledError) else "failed"
                )
            raise
        if trace and node:
            if response.tool_calls:
                trace.model_content(str(step.id), response, node_id=node)
            if not response.tool_calls:
                trace.finish(node)
        return step, node, response

    async def _tools(self, step, node, calls, context, names, child, child_node):
        runtime = self.bindings.runtime
        trace = self.bindings.state.trace
        assert runtime is not None
        reports = [call for call in calls if call.function.name == "report_to_parent"]
        if reports:
            if len(calls) != 1:
                return [
                    asdict(ToolResult(call.id, "error", '{"code":"report_must_be_alone"}'))
                    for call in calls
                ]
            try:
                report = ReportToParentInput.model_validate_json(reports[0].function.arguments).root
            except ValueError:
                return [asdict(ToolResult(reports[0].id, "error", '{"code":"invalid_report"}'))]
            if isinstance(report, FinalReport):
                child.returned = report.result
            elif trace and child_node:
                trace.update(child_node, summary=report.summary)
            return [asdict(ToolResult(reports[0].id, "ok", '{"accepted":true}'))]
        if step.prepared_request is not None:
            names = names.intersection(t.function.name for t in step.prepared_request.tools)
        if trace and node:
            trace.steps[str(step.id)] = node
            trace.bind_calls(str(step.id), calls, ())

        def started(prepared):
            if trace:
                trace.begin_call(str(step.id), prepared.call_id)

        def event(value):
            self.bindings.publish_execution(value)
            if trace and (identifier := trace.calls.get((str(step.id), value.tool_call_id))):
                trace.finish(
                    identifier,
                    "completed" if value.status in {"ok", "empty"} else "failed",
                    detail=trace.tool_detail(identifier, output_json=value.output_json),
                )

        child_context = replace(
            context,
            step_id=step.id,
            visible_names=names,
            caller="model",
            emit=event,
            on_start=started,
        )

        async def dispatch():
            results = []
            for item in await dispatch_batch(
                runtime.dispatcher, calls, child_context, ToolBatchPolicy()
            ):
                receipt = asdict(item)
                if item.proposal is not None:
                    receipt["proposal"] = item.proposal.model_dump(mode="json")
                # The model fingerprints the exact tool message, including JSON key order.
                receipt["observation_json"] = _tool_observation(receipt)
                results.append(receipt)
            return results

        results = await recorded(
            "tool",
            f"child-tools:{step.index}",
            {"calls": [call.model_dump(mode="json") for call in calls]},
            dispatch,
            list[dict],
        )
        journal = current_execution.get()
        if (getattr(journal, "context_binding", None) or {}).get("answerProtocol") == 2:
            from compass_agent.context_runtime.evidence import after_tool_batch, model_observation

            step.tool_calls = calls
            receipts = [
                ToolExecutionResult(**{k: v for k, v in item.items() if k != "observation_json"})
                for item in results
            ]
            observations = await asyncio.to_thread(
                after_tool_batch, journal, step, receipts, runtime.registry, context.trusted
            )
            for item, observed in zip(results, observations, strict=True):
                if observed["evidence"] or observed.get("feedback"):
                    item["observation_json"] = json_bytes(
                        model_observation(item["payload"], observed)
                    ).decode()
        for item in results:
            if item["cleanup"] == "unconfirmed":
                raise ResourceUnconfirmed()
            if item["status"] in {"canceled", "timeout"}:
                raise PhaseCanceled(cleanup_confirmed=True)
        return [
            asdict(
                ToolResult(
                    item["call_id"],
                    "ok" if item["status"] in {"ok", "empty"} else "error",
                    item["observation_json"]
                    if "observation_json" in item
                    else _tool_observation(item),
                )
            )
            for item in results
        ]
