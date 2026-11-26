"""Feature 992 flow acceptance with real application/runtime and controlled HTTP."""

import asyncio
import json
from uuid import uuid4

import pytest
from compass_agent.models import RoutingGrant
from compass_agent.models.errors import ModelError
from compass_agent.routing.dictionary import catalog, features_for, intent_for_scene
from compass_agent.routing.prompts import OFF_TOPIC_PROMPT
from compass_agent.routing.service import RoutingService
from compass_agent.schemas import ChatCallInput, Intent, MainModelSelection
from compass_agent.settings import Settings
from pydantic import ValidationError

from .retail_fixtures import RETAIL_PLAN_PRESET
from .test_models import CHAT, TEST, create, generate
from .test_models import system as system


def configured(system):
    repo, upstream, models = system
    pro = create(repo)[1].provider_id
    flash = create(
        repo, api_model="deepseek-flash", catalog_id="deepseek/deepseek-flash", alias="Flash"
    )[1].provider_id
    jev = create(
        repo,
        api_model="jev",
        catalog_id="typesafe/jev",
        alias="JEV",
        base_url="https://decision.example.com",
        chat_visible=False,
    )[1].provider_id
    settings = Settings(
        compass_context_runtime_enabled=False,
        compass_routing_jev_provider_id=jev,
        compass_routing_flash_provider_id=flash,
        compass_routing_pro_provider_id=pro,
    )
    return repo, upstream, RoutingService(models, settings), pro, flash, jev


def request(text="比较昨日销售额", provider=None, stream=False):
    return ChatCallInput(
        call_id=uuid4(),
        main_model=MainModelSelection(mode="fixed" if provider else "auto", provider_id=provider),
        input=generate(text).model_copy(update={"stream": stream}),
    )


def test_empty_optional_provider_environment_is_unconfigured(monkeypatch):
    for role in ("JEV", "FLASH", "PRO"):
        monkeypatch.setenv(f"COMPASS_ROUTING_{role}_PROVIDER_ID", "")
    settings = Settings()
    assert settings.compass_routing_jev_provider_id is None
    assert settings.compass_routing_flash_provider_id is None
    assert settings.compass_routing_pro_provider_id is None
    provider_id = uuid4()
    monkeypatch.setenv("COMPASS_ROUTING_FLASH_PROVIDER_ID", str(provider_id))
    assert Settings().compass_routing_flash_provider_id == provider_id
    monkeypatch.setenv("COMPASS_ROUTING_JEV_PROVIDER_ID", "invalid")
    with pytest.raises(ValidationError):
        Settings()


def test_intent_is_derived_from_scene_dictionary():
    assert len(catalog()[1]) == 40
    value = intent_for_scene("SC-02")
    assert set(value.model_dump()) == {"category_id", "scene_id"}
    assert value.category_id == "business_channel"
    assert features_for(value).difficulty == "L1"
    with pytest.raises(ValidationError):
        Intent.model_validate({**value.model_dump(), "difficulty": "L3"})
    with pytest.raises(ModelError, match="intent_invalid"):
        intent_for_scene("SC-999")


@pytest.mark.parametrize(
    "text,scene,role",
    [
        ("比较昨日销售额", "SC-02", "flash"),
        ("销售持续下降，分析原因", "SC-03", "pro"),
        ("你好呀，讲个笑话", "SC-39", "flash"),
    ],
)
def test_classify_dictionary_route_inference(system, text, scene, role):
    repo, upstream, routing, pro, flash, _ = configured(system)
    result = asyncio.run(routing.run(request(text), CHAT))
    assert result.status == "completed", result.model_dump()
    assert result.intent.scene_id == scene
    assert result.routing.provider_id == (pro if role == "pro" else flash)
    assert len(upstream.requests) == 2
    assert len(result.metrics.classification_calls) == 1
    assert result.metrics.classifier_source == "jev"
    assert result.metrics.usage_complete
    assert repo.call(result.call_id, CHAT).status == "completed"
    body = json.loads(upstream.requests[-1].content)
    system_prompt = body["messages"][0]["content"]
    environment = system_prompt.split("本轮运行环境\n")[1]
    classifier = json.loads(upstream.requests[0].content)
    assert environment in classifier["questions"]["scene"]["instructions"]
    assert CHAT.scope.kind == "tenant"
    assert str(CHAT.scope.tenant_id) in environment
    if scene == "SC-39":
        assert system_prompt.startswith(OFF_TOPIC_PROMPT + "\n\n")
        assert "业务数据" in result.result.text


def test_fixed_main_model_and_off_topic_override(system):
    _, _, routing, pro, flash, _ = configured(system)
    normal = asyncio.run(routing.run(request(provider=pro), CHAT))
    assert normal.routing.provider_id == pro
    assert normal.features.difficulty == "L1"
    casual = asyncio.run(routing.run(request("你好呀，讲个笑话", pro), CHAT))
    assert casual.routing.provider_id == flash
    assert casual.routing.reason == "off_topic_flash"


def test_retail_question_uses_normal_classification(system):
    _, upstream, routing, _, _, _ = configured(system)
    selected = asyncio.run(routing.select_route(request(RETAIL_PLAN_PRESET), CHAT))
    assert selected.metrics.classifier_source in {"flash", "jev"}
    assert upstream.requests
    assert selected.routing.reason.startswith("difficulty_")


def test_flash_baseline_and_duplicate_call_do_not_repeat_inference(system):
    _, upstream, routing, _, _, _ = configured(system)
    routing.settings.compass_routing_mode = "flash_only"
    command = request()
    result = asyncio.run(routing.run(command, CHAT))
    assert result.status == "completed" and result.metrics.classifier_source == "flash"
    assert len(upstream.requests) == 2
    with pytest.raises(ModelError, match="call_already_exists"):
        asyncio.run(routing.run(command, CHAT))
    assert len(upstream.requests) == 2


def test_jev_without_accepted_scene_falls_back_once(system, monkeypatch):
    _, upstream, routing, _, _, _ = configured(system)
    monkeypatch.setattr("compass_agent.routing.service.from_jev", lambda _: None)
    result = asyncio.run(routing.run(request(), CHAT))
    assert result.status == "completed"
    assert result.metrics.classifier_source == "flash"
    assert len(upstream.requests) == 3


@pytest.mark.parametrize("mode,source", [("flash_only", "flash"), ("jev_with_flash", "jev")])
def test_compound_scene_routes_with_its_dictionary_category(system, monkeypatch, mode, source):
    _, upstream, routing, pro, _, _ = configured(system)
    routing.settings.compass_routing_mode = mode
    monkeypatch.setattr(upstream, "routing_scene", lambda _: "SC-40")
    result = asyncio.run(routing.run(request("比较两周渠道实付金额，再分析主要商品的贡献。"), CHAT))
    assert result.status == "completed"
    assert result.intent == Intent(category_id="input_boundary", scene_id="SC-40")
    assert result.metrics.classifier_source == source
    assert result.routing.provider_id == pro


def test_bad_flash_output_stops_before_inference(system, monkeypatch):
    _, upstream, routing, _, _, _ = configured(system)
    routing.settings.compass_routing_mode = "flash_only"
    monkeypatch.setattr(
        "compass_agent.routing.service.from_flash",
        lambda _: intent_for_scene("SC-999"),
    )
    result = asyncio.run(routing.run(request(), CHAT))
    assert result.status == "failed" and result.error_code == "intent_invalid"
    assert result.result is None and len(upstream.requests) == 1


def test_internal_classifier_grant_cannot_change_scope(system):
    repo, _, routing, _, _, jev = configured(system)
    command = request()
    with pytest.raises(ModelError, match="forbidden"):
        repo.begin(
            command.call_id,
            jev,
            command.input,
            CHAT,
            grant=RoutingGrant(uuid4(), TEST.scope, command.call_id, jev, "generate"),
        )


def test_stream_routes_before_text_and_preserves_result(system):
    _, _, routing, _, _, _ = configured(system)
    events = []

    async def run():
        async def emit(event):
            events.append(event)

        return await routing.run(request(stream=True), CHAT, emit)

    result = asyncio.run(run())
    assert result.status == "completed"
    types = [event.type for event in events]
    assert types.index("route_selected") < types.index("text_delta")
    assert result.result.text == "".join(e.text for e in events if e.type == "text_delta")


def test_main_inference_failure_does_not_retry_or_change_model(system):
    _, upstream, routing, pro, _, _ = configured(system)
    result = asyncio.run(routing.run(request("E2E_AUTH_FAIL", pro), CHAT))
    assert result.status == "failed"
    assert result.result.error_code == "model_authentication_failed"
    assert result.routing.provider_id == pro
    assert len(upstream.requests) == 2


def test_cancel_during_classification_never_starts_fallback_or_inference(system, monkeypatch):
    _, upstream, routing, _, _, _ = configured(system)

    async def scenario():
        entered = asyncio.Event()

        async def blocked(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(routing.models, "run", blocked)
        task = asyncio.create_task(routing.run(request(), CHAT))
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert len(upstream.requests) == 0


def test_flash_classifier_transient_failure_is_single_attempt(system):
    import httpx

    _, upstream, routing, _, _, _ = configured(system)
    routing.settings.compass_routing_mode = "flash_only"
    calls = []

    async def reject(request):
        calls.append(request)
        return httpx.Response(503, json={"error": {"message": "controlled unavailable"}})

    upstream.handle = reject
    result = asyncio.run(routing.run(request(), CHAT))
    assert result.status == "failed" and result.result is None
    assert len(calls) == 1
    assert len(result.metrics.classification_calls) == 1
    execution = result.metrics.classification_calls[0].execution
    assert execution is not None and execution.max_attempts == 1


def test_main_generation_recovers_without_repeating_classification(system):
    import httpx

    _, upstream, routing, pro, _, _ = configured(system)
    original = upstream.handle
    main_requests = []

    async def transient(request):
        body = json.loads(request.content)
        if body.get("model") == "deepseek-v4-pro":
            main_requests.append(body)
            if len(main_requests) == 1:
                upstream.requests.append(request)
                return httpx.Response(429, json={"error": {"message": "controlled transient"}})
        return await original(request)

    upstream.handle = transient
    result = asyncio.run(routing.run(request(provider=pro), CHAT))
    assert result.status == "completed" and result.result.execution is not None
    assert len(result.metrics.classification_calls) == 1
    assert len(result.result.execution.attempts) == 2
    assert len(upstream.requests) == 3
    assert main_requests[0]["messages"] == main_requests[1]["messages"]
