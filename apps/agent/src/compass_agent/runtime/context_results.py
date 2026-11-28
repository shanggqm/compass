"""Session-authorized manifests for the generic tool-result file store."""

import asyncio
import logging
from dataclasses import replace
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from compass_agent.context_runtime.source_access import allows, assistant_message
from compass_agent.execution_context import execution_path
from compass_agent.runtime.entities import AgentAttempt, AgentRun, AgentSession, AgentStep
from compass_agent.runtime.private_state import digest
from compass_agent.tools.contracts import ExecutionReceipt, ToolRejected, json_bytes
from compass_agent.tools.result_store import INLINE_TOKENS, ToolResultStore, source_run


def _manifest(value, ref):
    if not isinstance(value, dict):
        return None
    candidate = value.get("context_result") or value.get("contextResult")
    if isinstance(candidate, dict) and candidate.get("ref") == ref:
        return candidate
    for key in ("result", "receipt"):
        found = _manifest(value.get(key), ref)
        if found:
            return found
    return None


class ContextResults:
    def __init__(self, journal):
        self.journal = journal
        self.files = ToolResultStore(journal.store.source_settings.compass_context_results_root)
        self.session_id = journal.store.read_run(journal.run_id).session_id

    def dependencies(self):
        with Session(self.journal.store.engine) as tx:
            message = assistant_message(tx, self.journal.run_id)
            refs = list(message.source_refs or []) if message else []
        return refs or [
            {
                "kind": "transient",
                "epoch": str(self.journal.epoch),
                "capturedAt": datetime.now(UTC).isoformat(),
            }
        ]

    async def govern(self, receipt, context):
        if receipt.context_result is not None or receipt.payload is None:
            return receipt
        raw = json_bytes(receipt.payload).decode()
        if self.files.count_tokens(raw) <= INLINE_TOKENS:
            return receipt

        async def save():
            try:
                dependencies = (
                    list(receipt.source_deps)
                    if receipt.source_deps is not None
                    else (await asyncio.to_thread(self.dependencies))
                )
                # A generic producer has no certified operation-specific source.
                # Earlier database reads cannot authorize this unrelated result.
                if receipt.source_deps is None and not any(
                    ref.get("kind") == "transient" for ref in dependencies
                ):
                    dependencies.append(
                        {
                            "kind": "transient",
                            "epoch": str(self.journal.epoch),
                            "capturedAt": datetime.now(UTC).isoformat(),
                        }
                    )
                dependencies = [
                    {
                        **ref,
                        "epoch": str(self.journal.epoch),
                        "capturedAt": datetime.now(UTC).isoformat(),
                    }
                    if ref.get("kind") == "transient"
                    else ref
                    for ref in dependencies
                ]
                manifest = await asyncio.to_thread(
                    self.files.save,
                    receipt.payload,
                    session_id=self.session_id,
                    run_id=context.run_id,
                    step_id=context.step_id,
                    call_id=context.call_id,
                    source_deps=dependencies,
                )
                visible = self.files.receipt(
                    manifest,
                    status=receipt.status,
                    metadata=receipt.payload if isinstance(receipt.payload, dict) else None,
                )
                return {"payload": visible, "context_result": manifest}
            except OSError as error:
                # The external operation already completed. Preserve its status and
                # never turn a storage failure into permission to run it again.
                logging.getLogger(__name__).warning(
                    "context.result_file_unavailable errno=%s", error.errno
                )
                return {
                    "payload": {
                        "status": receipt.status,
                        "originalAvailable": False,
                        "code": "result_file_unavailable",
                        "bytes": len(raw.encode()),
                    },
                    "context_result": None,
                }

        saved = await self.journal.local_read(
            f"{execution_path.get()}/result:{context.step_id}:{context.call_id}",
            {"step": str(context.step_id), "payloadDigest": digest(receipt.payload)},
            save,
            kind="ctx_result",
            yield_boundary=False,
        )
        return replace(receipt, **saved, governed=True)

    def resolve(self, ref, trusted):
        origin_id = source_run(ref)
        with Session(self.journal.store.engine) as tx:
            origin = tx.get(AgentRun, origin_id)
            session = tx.get(AgentSession, self.session_id)
            scope = trusted.scope
            if (
                origin is None
                or origin.session_id != self.session_id
                or session is None
                or session.deleted_at is not None
                or session.owner_user_id != trusted.actor_user_id
                or session.tenant_id != scope.tenant_id
                or session.workspace_id != scope.workspace_id
                or origin.ordinal < session.context_start_ordinal
            ):
                raise ToolRejected("result_access_denied")
            attempts = tx.scalars(
                select(AgentAttempt)
                .join(AgentStep, AgentStep.accepted_attempt_id == AgentAttempt.id)
                .where(AgentAttempt.run_id == origin_id, AgentAttempt.status == "completed")
            )
            manifest = None
            for attempt in attempts:
                value = self.journal.store.private.open(
                    origin_id, "attempt-output", attempt.output_content
                )
                if digest(value) != attempt.output_sha256:
                    raise ToolRejected("result_record_invalid")
                manifest = _manifest(value, ref)
                if manifest:
                    break
        if manifest is None or manifest.get("sessionId") != str(self.session_id):
            raise ToolRejected("result_reference_unavailable")
        refs = manifest["sourceDeps"]
        if any(r.get("kind") == "transient" for r in refs) and origin_id != self.journal.run_id:
            raise ToolRejected("result_source_expired")
        if not allows(
            self.journal.store, trusted, refs, epoch=self.journal.epoch, raise_unavailable=True
        ):
            raise ToolRejected("result_access_denied")
        return manifest

    async def read(self, args, context):
        manifest = await asyncio.to_thread(self.resolve, args.ref, context.trusted)
        value = await asyncio.to_thread(
            self.files.read, manifest, offset=args.offset or 0, limit=args.limit, cursor=args.cursor
        )
        # Recheck before releasing a page if authorization changed during the read.
        await asyncio.to_thread(self.resolve, args.ref, context.trusted)
        from compass_agent.context_runtime.source_access import record_sources

        await asyncio.to_thread(record_sources, self.journal, manifest["sourceDeps"])
        return ExecutionReceipt(
            context.call_id, "ok", "confirmed", value, source_deps=tuple(manifest["sourceDeps"])
        )
