"""Owner-scoped Session metadata and a restart-safe, one-shot title job."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from unicodedata import category
from uuid import UUID, uuid5

from sqlalchemy import select, update

from compass_agent.llm.policy import LogicalPolicy
from compass_agent.models.errors import ModelError
from compass_agent.models.service import ModelService
from compass_agent.prompt_assets import prompt_text
from compass_agent.routing.service import RoutingService
from compass_agent.runtime.entities import AgentMessage, AgentRun, AgentSession
from compass_agent.runtime.store import TERMINAL, RuntimeStore, now, owner_filter, session_item
from compass_agent.schemas import (
    ChatMessage,
    GenerateInput,
    MainModelSelection,
    SessionMutation,
    SessionRename,
    TrustedContext,
)

logger = logging.getLogger(__name__)
TITLE_ATTEMPTS = 2
TITLE_LEASE = timedelta(seconds=45)


def update_session(
    store: RuntimeStore,
    session_id: UUID,
    command: SessionMutation,
    context: TrustedContext,
):
    rename = isinstance(command, SessionRename)
    kind = "session_rename" if rename else "session_delete"
    payload = {**command.model_dump(mode="json"), "sessionId": str(session_id), "kind": kind}
    with store.transaction() as tx:
        old = store.command(tx, context, command.request_id, payload)
        session = tx.scalar(
            select(AgentSession)
            .where(
                AgentSession.id == session_id, *owner_filter(context, include_deleted=not rename)
            )
            .with_for_update()
        )
        if session is None:
            raise ModelError("not_found", 404)
        if old:
            return old.response
        run = tx.scalar(
            select(AgentRun)
            .where(AgentRun.session_id == session.id)
            .order_by(AgentRun.ordinal.desc())
            .limit(1)
        )
        if rename:
            title = " ".join(command.title.split())
            if not title or any(category(char) == "Cc" for char in title):
                raise ModelError("invalid_session_title", 422)
            session.title = title
            session.title_status = "manual"
            session.title_next_at = None
            session.version += 1
            receipt = session_item(session, run)
        else:
            if session.deleted_at is None:
                if run is not None and (
                    run.status not in TERMINAL
                    or run.public_view["run"]["status"] not in TERMINAL
                    or run.cleanup_status == "unconfirmed"
                ):
                    raise ModelError("session_busy", 409)
                if session.title_status == "pending":
                    session.title_status = "fallback"
                    session.title_next_at = None
                from compass_agent.goal.controller import cancel_session_goals

                cancel_session_goals(tx, session.id)
                session.deleted_at = now()
                session.version += 1
            receipt = {"sessionId": str(session.id)}
        store.record_command(
            tx, context, command.request_id, kind, payload, session.id, None, receipt
        )
    if not rename:
        from compass_agent.tools.result_store import ToolResultStore

        try:
            ToolResultStore(store.source_settings.compass_context_results_root).remove_session(
                session_id
            )
        except OSError:
            logger.warning(
                "Deleted session result files await storage cleanup",
                extra={"session_id": str(session_id)},
            )
    return receipt


@dataclass(frozen=True)
class TitleJob:
    session_id: UUID
    attempt: int
    question: str
    selection: MainModelSelection
    context: TrustedContext
    prompt: str | None = None


def title_prompt(tx, snapshot):
    binding = snapshot.get("contextRuntime")
    if binding is None:
        return prompt_text("support/session/title.md")
    from compass_agent.promptops.reader import PromptReader

    return PromptReader.load_exact(tx, binding["generation"], ["support/session/title.md"])[0].body


def claim_title(store: RuntimeStore) -> TitleJob | None:
    with store.transaction() as tx:
        timestamp = now()
        while True:
            session = tx.scalar(
                select(AgentSession)
                .where(
                    AgentSession.title_status == "pending",
                    AgentSession.title_next_at <= timestamp,
                )
                .order_by(AgentSession.title_next_at, AgentSession.id)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if session is None:
                return None
            # The first Run and its user message are committed with the Session.
            row = tx.execute(
                select(AgentRun.config_snapshot, AgentMessage.public_content)
                .join(AgentMessage, AgentMessage.run_id == AgentRun.id)
                .where(
                    AgentRun.session_id == session.id,
                    AgentRun.ordinal == 1,
                    AgentMessage.role == "user",
                    AgentMessage.channel == "conversation",
                    AgentMessage.ordinal == 1,
                )
            ).one_or_none()
            if (
                session.title_attempts >= TITLE_ATTEMPTS
                or row is None
                or not row.public_content
                or not row.public_content.get("text")
            ):
                session.title_status = "fallback"
                session.title_next_at = None
                session.version += 1
                tx.flush()
                continue
            session.title_attempts += 1
            session.title_next_at = timestamp + TITLE_LEASE
            return TitleJob(
                session.id,
                session.title_attempts,
                row.public_content["text"],
                MainModelSelection.model_validate(row.config_snapshot["command"]["selection"]),
                TrustedContext.model_validate(row.config_snapshot["context"]),
                title_prompt(tx, row.config_snapshot),
            )


def finish_title(store: RuntimeStore, job: TitleJob, title: str | None, *, retry=True) -> None:
    if title:
        fields = {"title": title, "title_status": "generated", "title_next_at": None}
    elif retry and job.attempt < TITLE_ATTEMPTS:
        fields = {"title_next_at": now() + timedelta(seconds=3)}
    else:
        fields = {"title_status": "fallback", "title_next_at": None}
    with store.transaction() as tx:
        tx.execute(
            update(AgentSession)
            .where(
                AgentSession.id == job.session_id,
                AgentSession.deleted_at.is_(None),
                AgentSession.title_status == "pending",
                AgentSession.title_attempts == job.attempt,
            )
            .values(**fields, version=AgentSession.version + 1)
        )


def short_title(output: str) -> str | None:
    title = output.strip().strip("`\"'“”‘’「」《》").strip()
    if (
        0 < len(title) <= 40
        and len(title.splitlines()) == 1
        and all(category(char) != "Cc" for char in title)
        and not title.startswith(("#", "标题:", "标题：", "Title:"))
    ):
        return title
    return None


async def generate_title(job: TitleJob, models: ModelService) -> str | None:
    provider_id = job.selection.provider_id
    if provider_id is None:
        provider_id, _ = await RoutingService(models).provider("flash")
    value = GenerateInput(
        messages=[
            ChatMessage(
                role="system", content=job.prompt or prompt_text("support/session/title.md")
            ),
            ChatMessage(role="user", content=job.question),
        ],
        max_tokens=96,
    )
    models.stage = "session_title"
    prepared = await models.prepare(
        uuid5(job.session_id, f"title:{job.attempt}"),
        provider_id,
        value,
        job.context,
        deadline=asyncio.get_running_loop().time() + 10,
    )
    result = await models.run(prepared, value, logical_policy=LogicalPolicy(max_attempts=1))
    return short_title(result.text) if result.status == "completed" else None


async def process_titles(
    store: RuntimeStore,
    check_access: Callable[[TrustedContext], Awaitable[object]],
    model_factory: Callable[[], ModelService],
    ready: Callable[[], bool],
) -> None:
    while True:
        try:
            job = await asyncio.to_thread(claim_title, store) if ready() else None
            if job is None:
                await asyncio.sleep(1)
                continue
            title, retry = None, True
            try:
                async with asyncio.timeout(15):
                    await check_access(job.context)
                    title = await generate_title(job, model_factory())
            except ModelError as error:
                retry = error.status != 403
            except TimeoutError:
                pass
            except Exception:
                logger.warning("session_title.generation_failed")
            await asyncio.to_thread(finish_title, store, job, title, retry=retry)
        except Exception:
            logger.warning("session_title.persistence_unavailable")
            await asyncio.sleep(1)
