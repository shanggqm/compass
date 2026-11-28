"""Recheck the retained inputs before rendering, continuing, or exporting a Run."""

import time
from datetime import UTC, datetime
from uuid import UUID, uuid5

import psycopg
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from compass_agent.data_connections.connections import DataConnections
from compass_agent.data_connections.entities import DataConnection
from compass_agent.data_connections.retail_access_refs import RETAIL_POLICY, expected_policy
from compass_agent.models.errors import ModelError
from compass_agent.runtime.entities import AgentMessage, AgentRun
from compass_agent.schemas import RUN_STREAM_LIMITS, TenantScope

RESTRICTED = (
    "这份结果的来源当前不可读取。请重新查询。"
    "如仍缺少访问权限，请联系工作区管理员申请该结果所用数据的读取权限。"
)
REFERENCE = "结果已保存。"
# A semantic discovery can admit 50 items, plus the class guard. Keep capacity
# for initial context references independently of the owner's HTTP page size.
MAX_SEARCH_REFS = 64 + 51 * RUN_STREAM_LIMITS["steps"] * RUN_STREAM_LIMITS["toolsPerStep"]


def assistant_message(tx, run_id):
    return tx.scalar(
        select(AgentMessage).where(
            AgentMessage.run_id == run_id,
            AgentMessage.channel == "conversation",
            AgentMessage.role == "assistant",
        )
    )


def record_source(journal, ref):
    record_sources(journal, [ref])


def record_sources(journal, refs):
    with journal.store.transaction() as tx:
        _, run = journal.store.lock(tx, journal.run_id)
        record_sources_locked(journal.store, tx, run, refs, journal.epoch)


def record_sources_locked(store, tx, run, incoming, epoch):
    if not run.config_snapshot.get("contextRuntime"):
        return
    store.require_epoch(run, epoch)
    message = assistant_message(tx, run.id)
    refs = list(message.source_refs or [])
    search = run.config_snapshot.get("searchSnapshot")
    if search and {"kind": "source_set_blocked"} in refs:
        return
    for ref in incoming:
        if ref.get("kind") == "transient":
            if any(r.get("kind") == "transient" and r.get("epoch") == str(epoch) for r in refs):
                continue
            ref = {**ref, "epoch": str(epoch), "capturedAt": datetime.now(UTC).isoformat()}
        if ref not in refs:
            refs.append(ref)
        if search and not ref.get("default_term"):
            kind = ref.get("kind")
            source = (
                "semantic"
                if kind in {"semantic", "semantic_result"}
                else "detail"
                if kind in {"retail", "metadata", "semantic_open"}
                else None
            )
            if source:
                guard = {
                    "kind": "search_source",
                    "runId": str(run.id),
                    "tenant": search["tenantId"],
                    "workspace": search["workspaceId"],
                    "sourceKind": source,
                }
                if guard not in refs:
                    refs.append(guard)
    # Default terms are reauthorized in batches independently of the tool reference budget.
    counted = sum(not (r.get("kind") == "semantic" and r.get("default_term")) for r in refs)
    modern = run.config_snapshot.get("contextRuntime", {}).get("answerProtocol") in {2, 3}
    if counted > (MAX_SEARCH_REFS if search or modern else 64):
        refs = [{"kind": "source_set_blocked" if search else "transient"}]
    message.source_refs = refs


def _verified_read(check):
    from compass_agent.data_connections.transport import DatabaseIdentityMismatch
    from compass_agent.tools.contracts import StopRequested, ToolRejected

    for attempt in range(2):
        try:
            return check()
        except (DatabaseIdentityMismatch, ToolRejected, StopRequested):
            return False
        except ModelError as exc:
            if exc.status < 500:
                return False
            if attempt:
                raise
        except (psycopg.OperationalError, TimeoutError) as exc:
            if attempt:
                raise ModelError("source_authorization_unavailable", 503) from exc
        except psycopg.Error:
            return False
        time.sleep(0.15)


def allows(store, context, refs, *, epoch=None, live_since=None, raise_unavailable=False):
    refs = refs or []
    if not refs:
        return True
    if not isinstance(context.scope, TenantScope):
        return False
    if any(r.get("kind") == "source_set_blocked" for r in refs):
        return False
    documents = [r for r in refs if r.get("kind") in {"document", "search_source"}]
    if documents:
        from compass_agent.documents.service import verify_documents

        try:
            if not _verified_read(lambda: verify_documents(store, context, documents)):
                return False
        except ModelError:
            if raise_unavailable:
                raise
            # A document owner outage also closes cached and historical projections.
            return False
    dictionary_refs = [r for r in refs if r.get("kind") == "metric_dictionary"]
    if dictionary_refs:
        with Session(store.engine) as tx:
            available = {
                str(key): rev
                for key, rev in tx.execute(
                    select(DataConnection.id, DataConnection.revision).where(
                        DataConnection.tenant_id == context.scope.tenant_id,
                        DataConnection.workspace_id == context.scope.workspace_id,
                    )
                )
            }
        if any(
            r["tenant"] != str(context.scope.tenant_id)
            or r["workspace"] != str(context.scope.workspace_id)
            or available.get(r["connection"]) != r["revision"]
            for r in dictionary_refs
        ):
            return False
    semantic = [r for r in refs if r.get("kind") in {"semantic", "semantic_result"}]
    open_results = [r for r in refs if r.get("kind") == "semantic_open"]
    if open_results:
        import asyncio

        from compass_agent.semantic.service import verify_open

        if not all(
            _verified_read(lambda r=r: asyncio.run(verify_open(store, context, r)))
            for r in open_results
        ):
            return False
    if semantic:
        from compass_agent.semantic.service import read_sources

        if not _verified_read(lambda: read_sources(store, context, semantic)):
            return False
    persistent = [r for r in refs if r.get("kind") in {"retail", "metadata"}]
    if any(
        r.get("kind")
        not in {
            "document",
            "search_source",
            "retail",
            "metadata",
            "metric_dictionary",
            "semantic",
            "semantic_result",
            "semantic_open",
        }
        and not (
            r.get("kind") == "transient"
            and r.get("epoch")
            and (r["epoch"] == str(epoch) or (live_since and r.get("capturedAt", "") >= live_since))
        )
        for r in refs
    ):
        return False
    if not persistent:
        return True
    try:
        bindings = {
            str(b.connection_id or b.workspace_id): b
            for b in DataConnections(store.engine, store.source_settings).bindings_for_run(
                context.scope
            )
        }
        grouped = {}
        for ref in persistent:
            binding = bindings.get(ref["connection"])
            if (
                binding is None
                or str(binding.tenant_id) != ref["tenant"]
                or str(binding.workspace_id) != ref["workspace"]
                or binding.role != ref["role"]
                or binding.revision != ref["revision"]
            ):
                return False
            grouped.setdefault(ref["connection"], []).append(ref)
        return all(_current_relations(bindings[key], group) for key, group in grouped.items())
    except (
        psycopg.errors.InsufficientPrivilege,
        psycopg.errors.UndefinedColumn,
        psycopg.errors.UndefinedTable,
    ):
        return False
    except psycopg.Error as exc:
        raise ModelError("source_authorization_unavailable", 503) from exc


def _current_relations(binding, refs):
    # Collect per-relation structural requirements and every distinct row witness.
    # These sets apply to one connection, revision and role in this authorization only.
    relations = {}
    witnesses = set()
    for ref in refs:
        for relation, columns in ref["columns"].items():
            policy = ("region_id" in columns) if ref["kind"] == "retail" else None
            relations.setdefault((relation, policy), set()).update(columns)
        witnesses.update((k["tenant"], k["region"]) for k in ref.get("keys", []))

    with psycopg.connect(
        binding.dsn.get_secret_value(),
        connect_timeout=3,
        options=(
            "-c default_transaction_read_only=on -c statement_timeout=3000 "
            "-c search_path=pg_catalog"
        ),
    ) as conn:
        if conn.execute("SELECT session_user, current_user").fetchone() != (
            binding.role,
            binding.role,
        ):
            return False
        for (relation, policy), columns in relations.items():
            checks = conn.execute(
                "SELECT pg_catalog.has_schema_privilege(relnamespace, 'USAGE'), "
                "pg_catalog.has_any_column_privilege(oid,'SELECT') AND NOT EXISTS "
                "(SELECT FROM unnest(%s::text[]) c "
                "WHERE NOT pg_catalog.has_column_privilege(oid,c,'SELECT')), "
                "pg_catalog.row_security_active(oid) FROM pg_catalog.pg_class "
                "WHERE oid=pg_catalog.to_regclass(%s)",
                (sorted(columns), relation),
            ).fetchone()
            if not checks or checks[:2] != (True, True):
                return False
            if policy is not None and (
                not checks[2]
                or conn.execute(RETAIL_POLICY, (expected_policy(policy), relation)).fetchone()
                != (True,)
            ):
                return False
        if witnesses:
            tenants, regions = zip(*witnesses, strict=True)
            if conn.execute(
                "SELECT NOT EXISTS(SELECT FROM unnest(%s::smallint[],%s::bigint[]) "
                "AS k(tenant,region) "
                "WHERE sec.allowed(k.tenant,k.region) IS DISTINCT FROM true)",
                (list(tenants), list(regions)),
            ).fetchone() != (True,):
                return False
        return True


def source_denial(refs):
    # Return only a public resource category, never a protected name or row identifier.
    categories = {
        "document": "document_access_denied",
        "retail": "database_access_denied",
        "metadata": "database_access_denied",
        "semantic_open": "database_access_denied",
        "semantic": "metric_access_denied",
        "semantic_result": "metric_access_denied",
        "metric_dictionary": "metric_access_denied",
        "transient": "source_result_expired",
    }
    kinds = {categories.get(ref.get("kind")) for ref in refs}
    reason = kinds.pop() if len(kinds) == 1 else None
    return ModelError("source_access_denied", 403, reason_code=reason)


def require_run_sources(store, run_id, context, *, epoch=None):
    with Session(store.engine) as tx:
        message = assistant_message(tx, run_id)
        refs = message.source_refs if message else []
    if not allows(store, context, refs, epoch=epoch, raise_unavailable=True):
        raise source_denial(refs)


def require_search_sources(journal, context, *, refs=None, epoch=None):
    """Authorize a private reconstruction as one operation until the first new node.

    New model frames, physical calls, cached reads in new nodes and public reads
    check the live policy independently. This grant exists only on this journal.
    """
    from compass_agent.runtime.private_state import digest

    if refs is None:
        with Session(journal.store.engine) as tx:
            message = assistant_message(tx, journal.run_id)
            refs = (message.source_refs if message else None) or []
    rebuilding = getattr(journal, "rebuilding_search", False)
    epoch_scope = str(epoch) if any(r.get("kind") == "transient" for r in refs) else None
    signature = digest([context.model_dump(mode="json"), refs, epoch_scope]) if rebuilding else None
    if rebuilding and signature == journal.rebuilt_source_digest:
        return
    if not allows(journal.store, context, refs, epoch=epoch, raise_unavailable=True):
        raise source_denial(refs)
    if rebuilding:
        journal.rebuilt_source_digest = signature


def save_final_text(store, tx, run, assistant, answer):
    save_private(store, tx, run, "final-answer", {"text": answer}, assistant.source_refs)


def view_key(payload):
    return f"view/{payload['objectId']}/{payload['revision']}"


def save_view(store, tx, run, event):
    save_private(store, tx, run, view_key(event["payload"]), event, None)


def save_private(store, tx, run, purpose, value, refs):
    key = uuid5(run.id, "context:" + purpose)
    previous = tx.get(AgentMessage, key)
    if previous:
        if store.private.open(run.id, purpose, previous.private_content) != value:
            raise ModelError("delivery_conflict", 409)
        return
    ordinal = (
        tx.scalar(select(func.max(AgentMessage.ordinal)).where(AgentMessage.run_id == run.id)) or 0
    )
    tx.add(
        AgentMessage(
            id=key,
            run_id=run.id,
            ordinal=ordinal + 1,
            channel="context",
            role="assistant",
            status="completed",
            revision=1,
            source_refs=refs,
            private_content=store.private.seal(run.id, purpose, value),
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
    )


def read_private(store, tx, run_id, purpose):
    message = tx.get(AgentMessage, uuid5(run_id, "context:" + purpose))
    if message and message.private_content:
        return store.private.open(run_id, purpose, message.private_content)
    return None


def final_text(store, tx, run_id):
    entry = read_private(store, tx, run_id, "final-answer")
    return entry["text"] if entry else None


def project_sources(store, response, context, *, live_since=None):
    from compass_agent.runtime.answer_publication import draft_text, hydrate_chunk, marker_data

    value = response.model_dump(mode="json", by_alias=True)
    decisions = {}
    statuses = {}
    denied_status = {}
    unavailable = set()
    search_runs = set()
    visible = []
    runs = {}
    with Session(store.engine) as tx:
        for packet in value["packets"]:
            identifier = UUID(packet["runId"])
            if identifier not in decisions:
                run = tx.get(AgentRun, identifier)
                runs[identifier] = run
                if not run or not run.config_snapshot.get("contextRuntime"):
                    decisions[identifier] = None
                else:
                    if run.config_snapshot.get("searchSnapshot"):
                        search_runs.add(identifier)
                    statuses[identifier] = run.public_view["run"]
                    message = assistant_message(tx, identifier)
                    refs = (message.source_refs if message else None) or []
                    try:
                        decisions[identifier] = allows(
                            store, context, refs, live_since=live_since, raise_unavailable=True
                        )
                    except ModelError as exc:
                        if exc.status < 500:
                            raise
                        decisions[identifier] = False
                        unavailable.add(identifier)
                    if not decisions[identifier] and statuses[identifier]["status"] == "failed":
                        denied_status[identifier] = (
                            "source_authorization_unavailable"
                            if identifier in unavailable
                            else source_denial(refs).reason_code or "restricted_run_result"
                        )
                        statuses[identifier] = {
                            **statuses[identifier],
                            "reasonCode": denied_status[identifier],
                        }
            allowed = decisions[identifier]
            event = packet["event"]
            if allowed is not None:
                payload = event["payload"]
                if data := marker_data(event):
                    if response.mode == "snapshot":
                        continue
                    packet["event"] = (
                        hydrate_chunk(store, tx, identifier, data)
                        if allowed
                        else {"type": "run.upsert", "payload": statuses[identifier]}
                    )
                    visible.append(packet)
                    continue
                if (
                    event["type"] == "run.upsert"
                    and payload["status"] == "failed"
                    and identifier in denied_status
                ):
                    payload["reasonCode"] = denied_status[identifier]
                if event["type"] == "message.upsert" and payload["role"] == "assistant":
                    if not allowed:
                        payload["content"] = {
                            **payload["content"],
                            "text": (
                                ""
                                if identifier in denied_status
                                else "数据访问权限暂时无法核验，请稍后刷新。"
                                if identifier in unavailable
                                else RESTRICTED
                            ),
                            "artifactIds": [],
                        }
                    else:
                        payload["content"]["text"] = (
                            (
                                final_text(store, tx, identifier)
                                if payload["status"] in {"completed", "incomplete"}
                                else None
                            )
                            or draft_text(store, tx, runs[identifier], payload)
                            or payload["content"]["text"]
                        )
                elif allowed and event["type"] == "view.upsert" and payload["kind"] != "artifact":
                    hydrated = read_private(store, tx, identifier, view_key(payload)) or event
                    if identifier in search_runs:
                        from compass_agent.runtime.public_visibility import search_event

                        hydrated = search_event(hydrated)
                    packet["event"] = hydrated
                elif not allowed and event["type"] not in {"run.upsert", "message.upsert"}:
                    if response.mode == "snapshot":
                        continue
                    # The delta protocol acknowledges each Redis sequence. A status-only
                    # packet advances that cursor even if every view in a batch is hidden.
                    packet["event"] = {"type": "run.upsert", "payload": statuses[identifier]}
            visible.append(packet)
    value["packets"] = visible
    return type(response).model_validate(value)


def sandbox_sources(store, context, run_ids, *, epoch=None):
    from compass_agent.runtime.entities import AgentSession

    scope = context.scope
    if not isinstance(scope, TenantScope):
        raise ModelError("source_access_denied", 403)
    owner = (scope.tenant_id, scope.workspace_id, context.actor_user_id)
    refs = []
    with Session(store.engine) as tx:
        for run_id in run_ids:
            run = tx.get(AgentRun, run_id)
            owner_session = tx.get(AgentSession, run.session_id) if run else None
            message = assistant_message(tx, run_id)
            if (
                owner_session is None
                or message is None
                or owner_session.deleted_at is not None
                or (
                    owner_session.tenant_id,
                    owner_session.workspace_id,
                    owner_session.owner_user_id,
                )
                != owner
            ):
                raise ModelError("source_access_denied", 403)
            for ref in message.source_refs or []:
                if ref not in refs:
                    refs.append(ref)
    if not allows(store, context, refs, epoch=epoch, raise_unavailable=True):
        raise source_denial(refs)
    return refs
