"""Transport choices on a private search frame, including replay and native SDK adaptation."""

import asyncio
import json
from datetime import date
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from compass_agent.context_runtime import model_step
from compass_agent.context_runtime.search_claims import SearchDraft
from compass_agent.execution_context import execution_scope
from compass_agent.harness.executor import AgentExecutor
from compass_agent.models.service import create_runtime
from compass_agent.runtime.context import RunRoute
from compass_agent.schemas import ChatMessage, ModelTokenLimits

from .test_models import CHAT, create, generate
from .test_models import system as system
from .test_sdk_protocols import native_response


class Frame:
    def __init__(self, search=True):
        self.run_id = uuid4()
        self.context_binding = {"scopeProfile": "operations", "generation": 1}
        if search:
            self.context_binding["search"] = {"kinds": ["history"]}
        self.context_phase = "query"
        self.context_date = date.today()
        self.context_receipts = {}
        self.context_questions = {}
        self.capabilities = None
        self.store = SimpleNamespace(
            metric_catalog={},
            source_settings=SimpleNamespace(compass_context_security_mode="enforce"),
        )
        self.saved = None

    def has_step(self, key):
        return self.saved is not None

    def restore_continuation(self, request, continuation):
        return request

    async def call(
        self, kind, key, inputs, invoke, result_type, *, frame_factory, receive_frame, **rest
    ):
        existed = self.saved is not None
        if not existed:
            self.saved = frame_factory(None)
        receive_frame(self.saved, existed)
        return await invoke()


def assemble(monkeypatch):
    def prepare(_tx, ctx, binding, draft, *args, **kwargs):
        return SimpleNamespace(
            request=draft,
            receipt={"generation": binding["generation"], "task": ctx.task, "phase": ctx.phase},
        )

    monkeypatch.setattr(model_step, "prepare_input", prepare)


def route(provider_id):
    return RunRoute(
        provider_id,
        1,
        "测试模型",
        "analysis",
        "",
        True,
        False,
        deliverable="business_qa",
        limits=ModelTokenLimits(max_input_tokens=64000, max_output_tokens=8192),
    )


@pytest.mark.parametrize(
    "search,path,requested,expected",
    [
        (True, "root", True, False),
        (True, "root", False, False),
        (False, "root", True, True),
        (True, "root/subagent:child", True, True),
        (True, "root/workflow:child", True, True),
    ],
)
def test_transport_is_a_property_of_the_new_private_root_frame(
    monkeypatch, search, path, requested, expected
):
    assemble(monkeypatch)
    frame = Frame(search)
    draft = generate(stream=requested)
    step = SimpleNamespace(id=uuid4())
    seen = []

    async def live(step, prepared, *args, publish_text):
        seen.append((prepared.stream, publish_text))

    with execution_scope(path, absolute=True):
        execution = SimpleNamespace(_invoke_model_live=live)

        def call():
            return model_step.call_model(
                frame, execution, step, draft, model_route, CHAT, None, None, None
            )

        model_route = route(uuid4())
        asyncio.run(call())
        assert frame.saved is not None
        assert frame.saved["input"]["request"]["stream"] is expected
        assert seen == [(expected, False)]
        # A preexisting streaming frame must be replayable with its original signed request.
        frame.saved["input"]["request"]["stream"] = True
        asyncio.run(call())
        assert seen[-1] == (True, False) and draft.stream is requested


def test_reopened_search_observations_use_a_fresh_non_reasoning_call(monkeypatch):
    assemble(monkeypatch)
    frame = Frame()
    draft = generate(thinking=True).model_copy(
        update={"messages": [ChatMessage(role="tool", tool_call_id="read", content="observed")]}
    )

    async def live(step, prepared, *args, publish_text):
        assert prepared.thinking is False
        assert prepared.reasoning_effort is None

    with execution_scope("root", absolute=True):
        asyncio.run(
            model_step.call_model(
                frame,
                SimpleNamespace(_invoke_model_live=live),
                SimpleNamespace(id=uuid4()),
                draft,
                route(uuid4()),
                CHAT,
                None,
                None,
                None,
            )
        )
    assert draft.thinking is True


@pytest.mark.parametrize(
    "vendor,model,base",
    [
        ("deepseek", "deepseek-v4-pro", "https://api.deepseek.com"),
        ("anthropic", "claude-sonnet-4-5", "https://api.anthropic.com"),
        ("gemini", "gemini-2.5-flash", "https://generativelanguage.googleapis.com"),
    ],
)
def test_long_private_draft_crosses_native_provider_without_sse_frames(
    system, monkeypatch, vendor, model, base
):
    assemble(monkeypatch)
    repo, _, service = system
    repo.allowed_origins += "," + base
    _, saved = create(repo, catalog_id=vendor + "/" + model, api_model=model, base_url=base)
    blocks = [f"item{i}" for i in range(8)]
    text = json.dumps(
        {
            "targets": [{"key": k} for k in blocks],
            "claimProposals": [
                {
                    "key": k,
                    "op": "quote",
                    "evidenceId": str(uuid4()),
                    "quote": "A signed source. " * 80,
                }
                for k in blocks
            ],
            "answerBlocks": blocks,
        }
    )
    assert len(text) > 10000
    SearchDraft.model_validate_json(text)
    sent = []

    def response(request):
        sent.append((str(request.url), json.loads(request.content)))
        if vendor == "deepseek":
            return httpx.Response(
                200,
                json={
                    "id": "response",
                    "object": "chat.completion",
                    "created": 1,
                    "model": model,
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": text},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 3, "completion_tokens": 3023, "total_tokens": 3026},
                },
            )
        payload = native_response(vendor, False).json()
        if vendor == "anthropic":
            payload["content"][0]["text"] = text
        else:
            payload["candidates"][0]["content"]["parts"][0]["text"] = text
        return httpx.Response(200, json=payload)

    service.runtime = create_runtime(repo.secrets, lambda: httpx.MockTransport(response))

    async def run():
        deadline = asyncio.get_running_loop().time() + 30
        return await model_step.call_model(
            Frame(),
            AgentExecutor(service),
            SimpleNamespace(id=uuid4(), model_call_id=uuid4()),
            generate(stream=True),
            route(saved.provider_id),
            CHAT,
            SimpleNamespace(phase_deadline=deadline, deadline_at=deadline),
            None,
            lambda event: pytest.fail("private draft reached public progress"),
        )

    result = asyncio.run(run())
    assert result.status == "completed" and result.text == text and result.usage.output_tokens
    assert (
        len(sent) == 1
        and not sent[0][1].get("stream")
        and "streamGenerateContent" not in sent[0][0]
    )
