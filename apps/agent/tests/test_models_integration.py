"""Runtime/configuration acceptance against an explicitly selected PostgreSQL database."""

import asyncio
import os
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from urllib.parse import urlsplit
from uuid import uuid4

import pytest
from compass_agent.llm.admission import ExecutionGate
from compass_agent.models.entities import ModelCall, ModelChange, Provider
from compass_agent.models.errors import ModelError
from compass_agent.models.repository import ModelRepository
from compass_agent.models.secrets import SecretStore
from compass_agent.models.service import ModelService, create_runtime
from compass_agent.schemas import ProviderPatch, TenantScope
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from .provider_fixture import ProviderFixture
from .test_database_tools_integration import db
from .test_database_tools_integration import targets as targets
from .test_models import CHAT, MANAGE, ORIGINS, TEST, create, generate, invoke


@pytest.fixture
def system(targets):
    url, _ = targets("models")
    db.migrate()
    key = os.environ.get("COMPASS_MODEL_ENCRYPTION_KEY", "")
    if not url or not key or not urlsplit(url).path.startswith("/compass_agent_test_"):
        pytest.fail("Explicit dedicated AGENT_TEST_DATABASE_URL and encryption key required")
    engine = create_engine(
        url.replace("postgresql://", "postgresql+psycopg://"), pool_pre_ping=True
    )
    repository = ModelRepository(engine, SecretStore(key), ORIGINS)
    fixture = ProviderFixture()
    yield (
        repository,
        fixture,
        ModelService(
            repository, create_runtime(repository.secrets, fixture.transport), gate=ExecutionGate()
        ),
    )
    engine.dispose()


def unique_create(repository, **kwargs):
    return create(repository, alias="PG " + uuid4().hex, **kwargs)


def concurrent(actions):
    barrier = Barrier(len(actions))

    def run(action):
        barrier.wait(timeout=5)
        try:
            return action()
        except ModelError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=len(actions)) as pool:
        return list(pool.map(run, actions))


def test_create_change_and_call_are_atomic_in_real_postgres(system):
    repo, upstream, service = system
    body, saved = unique_create(repo)
    assert repo.save(body, MANAGE) == saved
    result = asyncio.run(
        invoke(service, saved.provider_id, generate("synthetic business input"), CHAT)
    )
    assert result.status == "completed" and len(upstream.requests) == 1
    assert repo.call(result.call_id, CHAT).status == "completed"
    with Session(repo.engine) as session:
        row = session.get(Provider, saved.provider_id)
        change = session.get(ModelChange, body.operation_id)
        call = session.get(ModelCall, result.call_id)
        assert row is not None and change is not None and call is not None
        assert row.api_key_ciphertext != body.api_key and body.api_key not in row.api_key_ciphertext
        assert isinstance(CHAT.scope, TenantScope)
        assert call.tenant_id == CHAT.scope.tenant_id
        assert call.config_version == row.version and call.status == "completed"
        assert "synthetic business input" not in str(call.__dict__)
    repo.save(
        ProviderPatch(operation_id=uuid4(), expected_version=1, chat_visible=False),
        MANAGE,
        saved.provider_id,
    )
    with pytest.raises(ModelError, match="model_unavailable"):
        repo.begin(uuid4(), saved.provider_id, generate(), CHAT)
    assert all(x.id != saved.provider_id for x in repo.available().items)


def test_concurrent_operation_replay_and_optimistic_conflicts(system):
    repo, _, _ = system
    body, saved = unique_create(repo)
    command = ProviderPatch(
        operation_id=uuid4(), expected_version=1, alias="PG renamed " + uuid4().hex
    )
    outputs = concurrent([lambda: repo.save(command, MANAGE, saved.provider_id)] * 2)
    assert outputs[0] == outputs[1]
    assert repo.get(saved.provider_id).version == 2
    edits = [
        ProviderPatch(operation_id=uuid4(), expected_version=2, enabled=value)
        for value in (True, False)
    ]
    outputs = concurrent(
        [lambda cmd=cmd: repo.save(cmd, MANAGE, saved.provider_id) for cmd in edits]
    )
    assert sum(value == "version_conflict" for value in outputs) == 1
    with Session(repo.engine) as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(ModelChange)
                .where(ModelChange.provider_id == saved.provider_id)
            )
            == 3
        )
    # New create: both transactions may observe the operation as absent before the unique insert.
    new = body.model_copy(update={"operation_id": uuid4(), "alias": "PG racing " + uuid4().hex})
    outputs = concurrent([lambda: repo.save(new, MANAGE)] * 2)
    assert outputs[0] == outputs[1]


def test_call_id_race_creates_one_record_and_scope_is_enforced(system):
    repo, _, _ = system
    _, saved = unique_create(repo)
    call_id = uuid4()
    results = concurrent([lambda: repo.begin(call_id, saved.provider_id, generate(), TEST)] * 2)
    assert sum(result == "call_already_exists" for result in results) == 1
    assert repo.call(call_id, TEST).status == "running"
    with pytest.raises(ModelError, match="not_found"):
        repo.call(call_id, CHAT)
    with Session(repo.engine) as session:
        assert (
            session.scalar(
                select(func.count()).select_from(ModelCall).where(ModelCall.call_id == call_id)
            )
            == 1
        )


def test_v2_prepare_cancel_retains_terminal_metadata_in_postgres(system):
    from .test_model_lifecycle import check_cancel_after_prepare_commit

    check_cancel_after_prepare_commit(system)


def test_v2_terminal_update_is_idempotent_and_rejects_conflicts_in_postgres(system):
    from .test_model_lifecycle import check_finish_idempotency

    check_finish_idempotency(system)


def test_routing_uses_real_postgres_for_classification_and_main_call(system):
    from compass_agent.routing.service import RoutingService
    from compass_agent.schemas import ChatCallInput, MainModelSelection
    from compass_agent.settings import Settings

    from .context_fixture import install_prompts

    repo, upstream, models = system
    install_prompts(repo.engine)
    pro = unique_create(repo)[1].provider_id
    flash = unique_create(repo, api_model="deepseek-flash", catalog_id="deepseek/deepseek-flash")[
        1
    ].provider_id
    jev = unique_create(
        repo,
        api_model="jev",
        catalog_id="typesafe/jev",
        chat_visible=False,
        base_url="https://decision.example.com",
    )[1].provider_id
    routing = RoutingService(
        models,
        Settings(
            compass_routing_jev_provider_id=jev,
            compass_routing_flash_provider_id=flash,
            compass_routing_pro_provider_id=pro,
        ),
    )
    command = ChatCallInput(
        call_id=uuid4(),
        main_model=MainModelSelection(mode="fixed", provider_id=pro),
        input=generate("你好呀，讲个笑话"),
    )
    result = asyncio.run(routing.run(command, CHAT))
    assert result.status == "completed", result
    assert result.intent.scene_id == "SC-39"
    assert result.routing.provider_id == flash and result.routing.reason == "off_topic_flash"
    with Session(repo.engine) as session:
        calls = session.scalars(
            select(ModelCall).where(
                ModelCall.actor_user_id == CHAT.actor_user_id,
                ModelCall.call_id.in_(
                    [result.call_id, result.metrics.classification_calls[0].call_id]
                ),
            )
        ).all()
        assert len(calls) == 2 and all(c.status == "completed" for c in calls)
        assert all("讲个笑话" not in str(c.__dict__) for c in calls)
    assert len(upstream.requests) == 2


def test_reliable_generation_persists_two_attempts_atomically_and_rejects_duplicate(system):
    import httpx
    from compass_agent.llm.policy import LogicalPolicy

    repo, upstream, service = system
    _, provider = unique_create(repo)
    original = upstream.handle

    async def transient(request):
        if not upstream.requests:
            upstream.requests.append(request)
            return httpx.Response(429, json={"error": {"message": "controlled transient"}})
        return await original(request)

    upstream.handle = transient
    call_id = uuid4()
    value = generate("reliable PostgreSQL acceptance")

    async def run():
        prepared = await service.prepare(call_id, provider.provider_id, value, CHAT)
        result = await service.run(prepared, value, logical_policy=LogicalPolicy(max_attempts=2))
        assert result.status == "completed" and result.execution is not None
        assert len(result.execution.attempts) == 2
        record = repo.call(call_id, CHAT)
        assert record.status == "completed" and record.execution == result.execution
        assert record.usage.input_tokens is None
        assert service.gate.snapshot()["available"] == service.gate.capacity
        with pytest.raises(ModelError, match="call_already_exists"):
            await service.prepare(call_id, provider.provider_id, value, CHAT)
        assert len(upstream.requests) == 2

    asyncio.run(run())
    from compass_agent.runtime.entities import ModelAttempt

    with Session(repo.engine) as session:
        call = session.get(ModelCall, call_id)
        attempts = session.scalars(
            select(ModelAttempt)
            .where(ModelAttempt.call_id == call_id)
            .order_by(ModelAttempt.number)
        ).all()
        assert call and call.content_status == "captured" and call.request_content
        request = repo.secrets.open(call.request_content.decode(), "model-request")
        assert request["value"]["messages"] == value.model_dump(mode="json")["messages"]
        assert len(attempts) == 2
        assert [item.status for item in attempts] == ["failed", "completed"]
        assert all(item.origin == "live" and item.sent for item in attempts)
        assert all(item.reserved_tokens > 0 and item.request_content is None for item in attempts)
        assert all(item.output_content and item.output_sha256 for item in attempts)
        assert call.output_content == attempts[1].output_content
        assert all(
            str(item.id) in {str(a.attempt_id) for a in repo.call(call_id, CHAT).execution.attempts}
            for item in attempts
        )
