"""Workflow as a tool through the real QuickJS engine and child Agent process."""

import asyncio
import json
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from compass_agent.runtime.context import compass_definition
from compass_agent.runtime.control import OwnerKey, RunRegistry
from compass_agent.runtime.service import RunService
from compass_agent.schemas import WorkflowAssetView
from compass_agent.tools.builtin.database_query.handler import registration
from compass_agent.workflows.assets import DYNAMIC_CONTRACT
from compass_agent.workflows.catalog import tool_name
from compass_agent.workflows.drafts import WorkflowDrafts

from .test_agent_run import CHAT, FakeModels, FakeRouting, model_result, run_command, selected_route
from .test_tool_dispatcher import call

PARAMETERS = {
    "type": "object",
    "properties": {"window": {"type": "string"}},
    "required": ["window"],
    "additionalProperties": False,
}
SOURCE = """
const lead = await agent("sub_agent", {prompt: "Read " + args.window});
const branches = await parallel([
  () => agent("sub_agent", {prompt: "Break down " + lead.answer}),
  () => agent("sub_agent", {prompt: "Check " + lead.answer}),
]);
return {status: "ok", result: [lead.answer, ...branches.map(x => x.answer)]};
"""


@pytest.mark.anyio
async def test_workflow_child_argument_error_is_actionable_and_can_be_repaired():
    from compass_agent.tools.errors import ActionableToolRejected
    from compass_agent.workflows.engine import execute

    from .test_tool_dispatcher import context

    received = []

    async def child(task, group, request):
        received.append(task)
        return {"summary": "完成"}

    ctx = context()
    wrong = (
        "const result = await agent('sub_agent', {tasks:[{prompt:'核对数据'}]}); "
        "return {status:'ok',result};"
    )
    with pytest.raises(ActionableToolRejected) as error:
        await execute(wrong, {"type": "object"}, {}, ctx, child)
    assert error.value.error.code == "invalid_workflow_child"
    assert error.value.error.field == "scriptSource.agent.input.tasks.0.name"
    assert not received
    repaired = (
        "const result = await agent('sub_agent', {tasks:[{name:'核对',objective:'核对数据'}]}); "
        "return {status:'ok',result};"
    )
    result, _ = await execute(repaired, {"type": "object"}, {}, ctx, child)
    assert result["status"] == "ok" and len(received) == 1


class MemoryAssets:
    def __init__(self):
        self.records = {}

    def active_tools(self, context, *, limit):
        return [
            value
            for (owner, _), value in self.records.items()
            if owner == OwnerKey.from_context(context)
        ][:limit]

    def get(self, context, identifier, *, active=False):
        return self.records[OwnerKey.from_context(context), identifier]


def response(name, args, identifier="call"):
    return model_result(
        tool_calls=(call(name, identifier, json.dumps(args)),), token="next", finish="tool_calls"
    )


class ParentAndChildModels(FakeModels):
    def __init__(self, parent_responses):
        super().__init__(parent_responses)
        self.children = []

    async def run(self, prepared, request, **kwargs):
        if "你是独立的分析子 Agent" not in request.messages[0].content:
            return await super().run(prepared, request, **kwargs)
        self.children.append(request)
        assert [tool.function.name for tool in request.tools] == ["database_query"]
        if len(request.messages) == 2:
            return response("database_query", {"op": "list_connections"})
        observation = json.loads(request.messages[-1].content)
        assert observation == {"connections": []}
        return model_result(text=request.messages[1].content)


def service_for(models, assets, drafts):
    async def provider(_context, _control, emit):
        return (registration((), emit=emit),)

    return RunService(
        cast(Any, models),
        cast(Any, FakeRouting(selected_route())),
        definition=compass_definition(),
        tools_provider=provider,
        workflow_assets=assets,
        workflow_drafts=drafts,
    )


async def run(models, assets, drafts, *, question="本次分析", owner=CHAT):
    service = service_for(models, assets, drafts)
    control = RunRegistry().register(OwnerKey.from_context(owner))
    events = []
    result = await service.run(
        run_command().model_copy(update={"question": question}), owner, control, events.append
    )
    return result, events


def create_call(source=SOURCE):
    return response(
        "create_workflow",
        {
            "name": "周期对比",
            "description": "核对比较窗口的变化与贡献。",
            "parameters": PARAMETERS,
            "scriptSource": source,
            "args": {"window": "November"},
        },
    )


@pytest.mark.anyio
@pytest.mark.parametrize("source", [SOURCE, f"async function (args) {{{SOURCE}}};"])
async def test_create_then_run_saved_asset_with_new_model_arguments(source):
    assets = MemoryAssets()
    drafts = WorkflowDrafts()
    first = ParentAndChildModels([create_call(source), model_result(text="已试跑，等待保存。")])
    result, events = await run(first, assets, drafts)
    assert result.status == "completed", result
    observation = json.loads(first.requests[-1][1].messages[-1].content)
    assert observation["saveStatus"] == "awaiting_confirmation", observation
    assert not assets.records
    assert observation["result"] == [
        "Read November",
        "Break down Read November",
        "Check Read November",
    ]
    assert len(first.children) == 6
    assert all(
        "上一问" not in (message.content or "")
        for request in first.children
        for message in request.messages
    )
    assert any(
        event.type == "stream.extension" and event.payload.data.get("kind") == "subagent"
        for event in events
    )
    draft = drafts.get(CHAT, UUID(observation["draftId"]))

    asset = WorkflowAssetView(
        assetId=uuid4(),
        name=draft.name,
        status="active",
        definitionVersion=1,
        entryContract=DYNAMIC_CONTRACT,
        metadataRevision=1,
        scriptSource=draft.script_source,
        parameters=draft.parameters,
        description=draft.description,
    )
    assets.records[OwnerKey.from_context(CHAT), asset.asset_id] = asset
    name = tool_name(asset.asset_id)
    second = ParentAndChildModels(
        [response(name, {"window": "December"}), model_result(text="本次对比完成。")]
    )
    result, events = await run(second, assets, drafts, question="请复盘另一个月")
    assert result.status == "completed"
    assert name in {tool.function.name for tool in second.requests[0][1].tools}
    assert json.loads(second.requests[-1][1].messages[-1].content)["result"][0] == "Read December"
    assert any(
        event.type == "stream.extension"
        and (event.payload.data.get("workflow") or {}).get("mode") == "reused"
        for event in events
    )
    foreign = ParentAndChildModels([model_result(text="普通分析")])
    await run(foreign, assets, drafts, owner=CHAT.model_copy(update={"actor_user_id": uuid4()}))
    assert name not in {tool.function.name for tool in foreign.requests[0][1].tools}


@pytest.mark.anyio
async def test_subagent_is_also_a_direct_tool_and_failed_script_cannot_offer_save():
    direct = ParentAndChildModels(
        [response("sub_agent", {"prompt": "查询可用连接"}), model_result(text="已完成。")]
    )
    result, _ = await run(direct, MemoryAssets(), WorkflowDrafts())
    assert result.status == "completed"
    assert json.loads(direct.requests[-1][1].messages[-1].content) == {"answer": "查询可用连接"}
    broken = ParentAndChildModels(
        [create_call("return await agent('unknown', {});"), model_result(text="脚本失败。")]
    )
    _result, events = await run(broken, MemoryAssets(), WorkflowDrafts())
    assert broken.requests[-1][1].messages[-1].content == (
        '{"status":"rejected","code":"invalid_workflow_child"}'
    )
    assert all(
        event.type != "stream.extension" or event.payload.name != "compass.workflow.node"
        for event in events
    )


@pytest.mark.anyio
async def test_cancel_stops_child_loop_and_never_offers_save():
    entered = asyncio.Event()
    settled = asyncio.Event()

    class WaitingChild(ParentAndChildModels):
        async def run(self, prepared, request, **kwargs):
            if "你是独立的分析子 Agent" not in request.messages[0].content:
                return await super().run(prepared, request, **kwargs)
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                settled.set()

    models = WaitingChild([create_call()])
    service = service_for(models, MemoryAssets(), WorkflowDrafts())
    control = RunRegistry().register(OwnerKey.from_context(CHAT))
    events = []
    task = asyncio.create_task(service.run(run_command(), CHAT, control, events.append))
    await asyncio.wait_for(entered.wait(), 10)
    control.request_stop("user")
    result = await asyncio.wait_for(task, 10)
    assert result.status == "canceled", result
    assert settled.is_set()
    assert all(
        event.type != "stream.extension" or event.payload.name != "compass.workflow.node"
        for event in events
    )


@pytest.mark.anyio
async def test_workflow_engine_passes_new_tasks_and_old_prompt_without_losing_task_contract():
    from compass_agent.tools.builtin.sub_agent import SubAgentInput
    from compass_agent.tools.errors import ActionableToolRejected
    from compass_agent.workflows.engine import execute

    from .test_tool_dispatcher import context

    received = []

    async def child(value, *_):
        received.append(value)
        return {"answer": "ok"}

    source = (
        "const x = await agent('sub_agent', {tasks:"
        "[{name:'核对', objective:'核对结果', inputRefs:[]}]}); "
        "return {status:'ok',result:x};"
    )
    result, _ = await execute(source, PARAMETERS, {"window": "November"}, context(), child)
    assert result["status"] == "ok"
    assert isinstance(received[0], SubAgentInput) and received[0].tasks
    assert received[0].tasks[0].objective == "核对结果"
    await execute(
        "const x = await agent('sub_agent', {prompt:'检查'}); return {status:'ok',result:x};",
        PARAMETERS,
        {"window": "November"},
        context(),
        child,
    )
    assert received[1] == "检查"
    with pytest.raises(ActionableToolRejected) as failure:
        await execute(source, PARAMETERS, {}, context(), child)
    assert failure.value.error.field == "args.window"
    assert failure.value.error.payload()["nextAction"]
