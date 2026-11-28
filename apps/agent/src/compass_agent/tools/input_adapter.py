"""Freeze deterministic input enrichment alongside existing Run private state."""

import asyncio
from hashlib import sha256

from compass_agent.execution_context import current_execution, execution_path
from compass_agent.tools.contracts import PhaseCanceled, ToolRejected, json_bytes


async def normalize_arguments(normalizer, args, context):
    """Run synchronous adapters with their own thread-bound reads and transactions."""
    task = asyncio.create_task(asyncio.to_thread(normalizer, args, context))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        # Joining metadata reads keeps cancellation from leaving an unowned transaction.
        await asyncio.gather(task, return_exceptions=True)
        raise PhaseCanceled(cleanup_confirmed=True) from None


def digest(value):
    return sha256(json_bytes(value)).hexdigest()


def frozen_input(name, raw, context, build):
    from compass_agent.context_runtime.source_access import read_private, save_private

    journal = current_execution.get()
    if journal is None:
        raise ToolRejected("tool_scope_denied")
    key = f"tool-input/v1/{execution_path.get()}/{context.step_id}/{context.call_id}/{name}"
    with journal.store.transaction() as tx:
        session, run = journal.store.lock(tx, journal.run_id, context.trusted)
        journal.store.require_epoch(run, journal.epoch)
        saved = read_private(journal.store, tx, run.id, key)
        if saved is not None:
            if saved["digest"] != digest(raw):
                raise ToolRejected("execution_replay_mismatch")
            return saved["args"]
        args = build(tx, session, run)
        save_private(journal.store, tx, run, key, {"digest": digest(raw), "args": args}, [])
        return args
