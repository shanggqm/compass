"""One owner-scoped environment, independent bounded commands, shared durable files."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from datetime import timedelta

from opensandbox import Sandbox, SandboxManager
from opensandbox.models import WriteEntry
from opensandbox.models.execd import RunCommandOpts
from opensandbox.models.sandboxes import SandboxFilter

from compass_agent.tools.contracts import ExecutionReceipt, SandboxBinding, ToolRejected, json_bytes
from compass_agent.tools.registry import bounded_payload
from compass_agent.tools.sandbox.client import SandboxBackend as LegacyBackend
from compass_agent.tools.sandbox.shared_allocations import COMMAND_SECONDS, SharedAllocations


class SandboxBackend(LegacyBackend):
    def __init__(self, allocations, *, idle_s=600, parallelism=2, **kwargs):
        super().__init__(allocations, **kwargs)
        self.shared = SharedAllocations(allocations, idle_s=idle_s, parallelism=parallelism)

    async def _connect(self, allocation):
        return await Sandbox.connect(
            allocation.sandbox_id, connection_config=self.connection, skip_health_check=True
        )

    async def _stop_command(self, sandbox, command_id):
        if not command_id:
            return False
        try:
            async with asyncio.timeout(8):
                try:
                    await sandbox.commands.interrupt(command_id)
                except Exception:
                    pass  # A lost interrupt response still permits status confirmation.
                while True:
                    status = await sandbox.commands.get_command_status(command_id)
                    if status.running is False:
                        return True
                    await asyncio.sleep(0.1)
        except (Exception, asyncio.CancelledError):
            return False

    async def execute(self, binding, args, context):
        if not isinstance(binding, SandboxBinding) or not isinstance(args, dict):
            return ExecutionReceipt(
                context.call_id, "rejected", "not_started", reason_code="invalid_tool_binding"
            )
        source = context.sources.get(args["source_query_id"])
        if source is None or source.owner != context.owner or source.run_id != context.run_id:
            return ExecutionReceipt(
                context.call_id, "rejected", "not_started", reason_code="sandbox_source_denied"
            )
        if source.revalidate is not None and not await context.control.perform(source.revalidate):
            return ExecutionReceipt(
                context.call_id, "rejected", "not_started", reason_code="sandbox_source_stale"
            )
        inputs, code = json_bytes(source.data), args["code"]
        if len(inputs) > 128 * 1024 or len(code.encode()) > 16 * 1024:
            return ExecutionReceipt(
                context.call_id, "rejected", "not_started", reason_code="sandbox_input_limit"
            )
        allocation = command = sandbox = None
        creator = started = finished = False
        remote_id = None
        status, reason, payload, chart = "error", "sandbox_failed", None, None
        try:
            async with asyncio.timeout(context.control.remaining):
                while True:
                    context.require_running()
                    # Cancellation cannot leave an unobserved local reservation.
                    task = asyncio.create_task(
                        asyncio.to_thread(
                            self.shared.acquire,
                            context.owner,
                            context.run_id,
                            context.step_id,
                            context.call_id,
                            hashlib.sha256(inputs + code.encode()).hexdigest(),
                        )
                    )
                    try:
                        reserved = await asyncio.shield(task)
                    except asyncio.CancelledError:
                        reserved = await task
                        if reserved:
                            allocation, command, creator = reserved
                        raise
                    if reserved:
                        allocation, command, creator = reserved
                        break
                    await asyncio.sleep(0.2)
                runs = await asyncio.to_thread(self.shared.source_runs, allocation)
                if context.sandbox_access is not None:
                    await context.sandbox_access(runs)
                elif any(run != context.run_id for run in runs):
                    raise ToolRejected("sandbox_source_stale")
                if creator:
                    started = True  # a lost create response still owns the environment lease
                    sandbox, allocation = await self._create(
                        allocation, read_only=False, remaining=context.control.remaining
                    )
                    started = False
                else:
                    sandbox = await self._connect(allocation)
                    from compass_agent.tools.sandbox.shared_allocations import RESOURCE_SECONDS

                    await sandbox.renew(timedelta(seconds=RESOURCE_SECONDS))
                directory = f"/workspace/executions/{command.id.hex}"
                prepared = await sandbox.commands.run(
                    f"mkdir -p {directory} /workspace/shared",
                    opts=RunCommandOpts(timeout=timedelta(seconds=5)),
                )
                if prepared.error or prepared.exit_code not in {0, None}:
                    raise ToolRejected("sandbox_prepare_failed")
                await sandbox.files.write_files(
                    [
                        WriteEntry(path=f"{directory}/source.py", data=code, mode=600),
                        WriteEntry(path=f"{directory}/input.json", data=inputs, mode=600),
                    ]
                )
                started = True
                execution = await sandbox.commands.run(
                    f"/usr/local/bin/python -I /opt/compass/runner.py {command.id.hex} "
                    f">{directory}/process.log 2>&1",
                    opts=RunCommandOpts(
                        background=True,
                        working_directory=directory,
                        timeout=timedelta(seconds=min(COMMAND_SECONDS, context.control.remaining)),
                    ),
                )
                if not execution.id:
                    raise ToolRejected("sandbox_command_unconfirmed")
                # Persist before waiting; a different replica can finish cleanup after a crash.
                remote_id = execution.id
                command = await asyncio.to_thread(self.shared.launched, command, remote_id)
                while True:
                    result = await sandbox.commands.get_command_status(remote_id)
                    if result.running is False:
                        finished = True
                        if result.exit_code != 0:
                            raise ToolRejected("sandbox_execution_failed")
                        break
                    await asyncio.sleep(0.15)
                collected = await sandbox.commands.run(
                    f"/usr/local/bin/python -I /opt/compass/collector.py {command.id.hex}",
                    opts=RunCommandOpts(timeout=timedelta(seconds=5)),
                )
                if collected.error or collected.exit_code not in {0, None}:
                    raise ToolRejected("sandbox_collection_failed")
                raw = "".join(line.text for line in collected.logs.stdout)
                if len(raw.encode()) > 800 * 1024:
                    raise ToolRejected("sandbox_collection_failed")
                envelope = json.loads(raw)
                payload = bounded_payload(envelope["result"])
                if envelope.get("chart") is not None:
                    chart = base64.b64decode(envelope["chart"], validate=True)
                elif envelope.get("chartRejected"):
                    chart = b"invalid"
                if context.sandbox_access is not None:
                    await context.sandbox_access(
                        await asyncio.to_thread(self.shared.source_runs, allocation)
                    )
                status, reason = "ok", None
        except asyncio.CancelledError:
            status, reason = "canceled", "sandbox_canceled"
        except TimeoutError:
            status, reason = "timeout", "sandbox_timeout"
        except ToolRejected as error:
            reason = error.code
            if allocation and reason in {"source_access_denied", "sandbox_source_stale"}:
                # Expired source grants must not poison all future work for this owner.
                # Drain admitted companions before deleting the inaccessible workspace.
                await asyncio.to_thread(self.shared.retire, allocation)
        except Exception:
            pass
        finally:

            async def finish():
                nonlocal finished
                if command is None:
                    return
                if not started:
                    finished = True
                    if creator and allocation and allocation.sandbox_id is None:
                        await asyncio.to_thread(self.allocations.cancel_before_create, allocation)
                elif not finished and sandbox:
                    finished = await self._stop_command(sandbox, remote_id or command.command_id)
                if not finished and creator and allocation and allocation.sandbox_id is None:
                    await asyncio.to_thread(self.allocations.unknown, allocation)
                await asyncio.to_thread(self.shared.finish, command, confirmed=finished)
                if sandbox:
                    await sandbox.close()

            cleanup = asyncio.create_task(finish())
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                try:
                    await cleanup
                except Exception:
                    finished = False
            except Exception:
                finished = False
        if command is None:
            return ExecutionReceipt(context.call_id, status, "not_started", reason_code=reason)
        return ExecutionReceipt(
            str(command.id),
            status if finished else "unknown",
            "confirmed" if finished else "unconfirmed",
            payload if status == "ok" and finished else None,
            file=chart if status == "ok" and finished else None,
            reason_code=reason if finished else "sandbox_command_unconfirmed",
            environment_id=allocation.sandbox_id if allocation else None,
        )

    def inspection_target(self, identifier, owner):
        from dataclasses import replace

        item = self.shared.inspection(identifier, owner)
        if item is None:
            return super().inspection_target(identifier, owner)
        allocation, command, run_id, step_id, call_id = item
        return replace(allocation, id=command.id, run_id=run_id, step_id=step_id, call_id=call_id)

    def inspection_runs(self, identifier, owner):
        item = self.shared.inspection(identifier, owner)
        if item is None:
            return super().inspection_runs(identifier, owner)
        return self.shared.source_runs(item[0])

    async def inspect(self, identifier, owner):
        item = await asyncio.to_thread(self.shared.inspection, identifier, owner)
        if item is None:
            return await super().inspect(identifier, owner)
        allocation, command, *_ = item
        sandbox = None
        try:
            async with asyncio.timeout(5):
                sandbox = await self._connect(allocation)
                collected = await sandbox.commands.run(
                    f"/usr/local/bin/python -I /opt/compass/collector.py {command.id.hex}",
                    opts=RunCommandOpts(timeout=timedelta(seconds=4)),
                )
                if collected.error or collected.exit_code not in {0, None}:
                    raise ToolRejected("sandbox_collection_failed")
                raw = "".join(line.text for line in collected.logs.stdout)
                if len(raw.encode()) > 800 * 1024:
                    raise ToolRejected("sandbox_collection_failed")
                envelope = json.loads(raw)
                chart = (
                    base64.b64decode(envelope["chart"], validate=True)
                    if envelope.get("chart")
                    else None
                )
                return ExecutionReceipt(
                    str(identifier),
                    "ok",
                    "confirmed",
                    bounded_payload(envelope["result"]),
                    file=chart,
                    environment_id=allocation.sandbox_id,
                )
        finally:
            if sandbox:
                await sandbox.close()

    async def confirmed_executions(self, identifiers):
        return [
            *await super().confirmed_executions(identifiers),
            *await asyncio.to_thread(self.shared.confirmed_executions, identifiers),
        ]

    async def reconcile(self):
        resolved = await super().reconcile()
        for allocation, command in await asyncio.to_thread(self.shared.due_commands):
            if not command.command_id:
                # Unknown submission cannot be retried. Reclaim its environment when companions end.
                await asyncio.to_thread(self.shared.finish, command, confirmed=False)
                continue
            sandbox = None
            try:
                sandbox = await self._connect(allocation)
                result = await sandbox.commands.get_command_status(command.command_id)
                confirmed = result.running is False or await self._stop_command(
                    sandbox, command.command_id
                )
                await asyncio.to_thread(self.shared.finish, command, confirmed=confirmed)
                if confirmed:
                    resolved.append(str(command.id))
            except Exception:
                await asyncio.to_thread(self.shared.finish, command, confirmed=False)
            finally:
                if sandbox:
                    await sandbox.close()
        for allocation in await asyncio.to_thread(self.shared.claim_due):
            try:
                if allocation.state != "retained":
                    manager = await SandboxManager.create(self.cleanup_connection)
                    try:
                        matches = await manager.list_sandbox_infos(
                            SandboxFilter(metadata=allocation.metadata, page_size=100, page=1)
                        )
                        from dataclasses import replace

                        if allocation.sandbox_id is None and len(matches.sandbox_infos) == 1:
                            allocation = await asyncio.to_thread(
                                self.allocations.observe_resource,
                                allocation,
                                matches.sandbox_infos[0].id,
                            )
                        if len(matches.sandbox_infos) >= 100:
                            continue
                        if not matches.sandbox_infos:
                            if allocation.sandbox_id:
                                if not await self._delete_confirmed(allocation):
                                    continue
                            elif not await asyncio.to_thread(
                                self.allocations.observe_absent, allocation
                            ):
                                continue
                        if not all(
                            [
                                await self._delete_confirmed(
                                    replace(allocation, sandbox_id=info.id)
                                )
                                for info in matches.sandbox_infos
                            ]
                        ):
                            continue
                        allocation = await asyncio.to_thread(self.shared.disposed, allocation)
                    finally:
                        await manager.close()
                allocation = await asyncio.to_thread(self.allocations.phase, allocation, "clean")
                await self._collect(allocation, 30, clean=True)
            except Exception:
                continue
        return resolved
