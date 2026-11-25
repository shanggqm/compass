"""Application composition of query tools from current workspace authorization."""

import asyncio

from compass_agent.schemas import TenantScope
from compass_agent.tools.builtin.database_query.handler import finish_bookkeeping, registration


def provider(connections):
    async def load(trusted, control, emit):
        if not isinstance(trusted.scope, TenantScope):
            return ()

        from compass_agent.documents.client import restrict_connections, verify
        from compass_agent.execution_context import current_execution

        journal = current_execution.get()
        snapshot = getattr(journal, "search_snapshot", None)
        if snapshot and "detail" not in snapshot["kinds"]:
            return ()

        async def current():
            if snapshot and not await finish_bookkeeping(
                asyncio.to_thread(
                    verify, journal.store.source_settings, trusted, journal.run_id, kinds=["detail"]
                )
            ):
                return ()

            return restrict_connections(
                await finish_bookkeeping(
                    asyncio.to_thread(connections.bindings_for_run, trusted.scope)
                ),
                snapshot,
            )

        bindings = await current()
        if snapshot and not bindings:
            return ()
        control.require_running()
        return (registration(bindings, emit=emit, current=current),)

    return load
