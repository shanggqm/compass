"""OpenSandbox lifecycle backend. Every execution and collector receives its own container."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from datetime import timedelta
from uuid import uuid5

from opensandbox import Sandbox, SandboxManager
from opensandbox.config import ConnectionConfig
from opensandbox.exceptions import SandboxApiException
from opensandbox.models import WriteEntry
from opensandbox.models.execd import RunCommandOpts
from opensandbox.models.sandboxes import PVC, NetworkPolicy, SandboxFilter, Volume
from opensandbox.transport.retry import RetryPolicy

from compass_agent.tools.contracts import (
    ExecutionBinding,
    ExecutionReceipt,
    PhaseCanceled,
    SandboxBinding,
    StopRequested,
    ToolExecutionContext,
    ToolRejected,
    json_bytes,
)
from compass_agent.tools.registry import bounded_payload
from compass_agent.tools.sandbox.allocations import Allocation, Allocations


class SandboxBackend:
    def __init__(
        self,
        allocations: Allocations,
        *,
        domain: str,
        api_key: str,
        image: str,
        volume_prefix: str = "compass-sandbox-slot",
    ):
        self.allocations, self.image, self.volume_prefix = allocations, image, volume_prefix
        self.connection = ConnectionConfig(
            domain=domain,
            api_key=api_key,
            request_timeout=timedelta(seconds=30),
            retry_policy=RetryPolicy.disabled(),
            follow_redirects=False,
            disable_metrics=True,
        )
        self.cleanup_connection = self.connection.model_copy(
            update={"request_timeout": timedelta(seconds=3)}
        )

    async def _create(self, allocation: Allocation, *, read_only: bool, remaining: float):
        if remaining <= 0:
            raise TimeoutError()
        sandbox = await Sandbox.create(
            self.image,
            timeout=timedelta(seconds=120),
            ready_timeout=timedelta(seconds=min(30, remaining)),
            resource={"cpu": "1", "memory": "512Mi"},
            metadata=allocation.metadata,
            network_policy=NetworkPolicy(defaultAction="deny"),
            volumes=[
                Volume(
                    name="workspace",
                    pvc=PVC(
                        claimName=f"{self.volume_prefix}{allocation.slot:03d}",
                        createIfNotExists=False,
                    ),
                    mountPath="/workspace",
                    readOnly=read_only,
                )
            ],
            connection_config=self.connection,
        )
        attached = await asyncio.to_thread(self.allocations.attach, allocation, sandbox.id)
        return sandbox, attached

    async def _delete_confirmed(self, allocation: Allocation) -> bool:
        if allocation.sandbox_id is None:
            return False
        manager = await SandboxManager.create(self.cleanup_connection)
        try:
            async with asyncio.timeout(8):
                try:
                    await manager.kill_sandbox(allocation.sandbox_id)
                except Exception:
                    # A lost delete response can still mean deletion is in progress.
                    # Use the remaining cleanup window to confirm the remote state.
                    pass
                while True:
                    try:
                        await manager.get_sandbox_info(allocation.sandbox_id)
                    except SandboxApiException as error:
                        if error.status_code == 404:
                            return True
                    except Exception:
                        pass
                    await asyncio.sleep(0.15)
        except (Exception, asyncio.CancelledError):
            return False
        finally:
            await manager.close()

    async def _finish(self, allocation: Allocation, *, clean: bool = False) -> Allocation | None:
        task = asyncio.create_task(self._delete_confirmed(allocation))
        try:
            confirmed = await asyncio.shield(task)
        except asyncio.CancelledError:
            confirmed = await task
        if confirmed:
            return await asyncio.to_thread(
                self.allocations.confirmed, allocation, volume_clean=clean
            )
        await asyncio.to_thread(self.allocations.unknown, allocation)
        return None

    async def _begin_phase(self, operation, *args) -> Allocation:
        reservation = asyncio.create_task(asyncio.to_thread(operation, *args))
        try:
            return await asyncio.shield(reservation)
        except asyncio.CancelledError:
            try:
                allocation = await reservation
            except ToolRejected:
                pass
            else:
                # Remote creation starts only after this method returns successfully.
                await asyncio.to_thread(self.allocations.cancel_before_create, allocation)
            raise PhaseCanceled(cleanup_confirmed=True) from None

    async def execute(
        self, binding: ExecutionBinding, args: object, context: ToolExecutionContext
    ) -> ExecutionReceipt:
        if not isinstance(binding, SandboxBinding) or not isinstance(args, dict):
            return ExecutionReceipt(
                context.call_id, "rejected", "not_started", reason_code="invalid_tool_binding"
            )
        source_id, code = args["source_query_id"], args["code"]
        source = context.sources.get(source_id)
        if source is None or source.owner != context.owner or source.run_id != context.run_id:
            return ExecutionReceipt(
                context.call_id, "rejected", "not_started", reason_code="sandbox_source_denied"
            )
        if source.revalidate is not None and not await context.control.perform(source.revalidate):
            return ExecutionReceipt(
                context.call_id, "rejected", "not_started", reason_code="sandbox_source_stale"
            )
        inputs = json_bytes(source.data)
        if len(inputs) > 128 * 1024 or len(code.encode()) > 16 * 1024:
            return ExecutionReceipt(
                context.call_id, "rejected", "not_started", reason_code="sandbox_input_limit"
            )
        context.require_running()
        attempt = uuid5(context.run_id, f"{context.step_id}:{context.call_id}")
        allocation = await self._begin_phase(
            self.allocations.reserve,
            context.owner,
            context.run_id,
            context.step_id,
            context.call_id,
            attempt,
            hashlib.sha256(inputs + code.encode()).hexdigest(),
        )
        sandbox = None
        status, reason = "error", "sandbox_failed"
        accepted = None
        environment = None
        try:
            async with asyncio.timeout(context.control.remaining):
                sandbox, allocation = await self._create(
                    allocation, read_only=False, remaining=context.control.remaining
                )
                environment = sandbox.id
                await sandbox.files.write_files(
                    [
                        WriteEntry(path="/workspace/source.py", data=code, mode=600),
                        WriteEntry(path="/workspace/input.json", data=inputs, mode=600),
                    ]
                )
                execution = await sandbox.commands.run(
                    "/usr/local/bin/python /opt/compass/runner.py >/workspace/process.log 2>&1",
                    opts=RunCommandOpts(
                        timeout=timedelta(seconds=min(20, context.control.remaining))
                    ),
                )
                status, reason = (
                    ("ok", None)
                    if execution.error is None and execution.exit_code in {0, None}
                    else ("error", "sandbox_execution_failed")
                )
        except asyncio.CancelledError:
            status, reason = "canceled", "sandbox_canceled"
        except TimeoutError:
            status, reason = "timeout", "sandbox_timeout"
        except Exception:
            pass
        finally:
            if sandbox:
                try:
                    await sandbox.close()
                except (Exception, asyncio.CancelledError):
                    pass
            accepted = await self._finish(allocation)
        if accepted is None:
            return ExecutionReceipt(
                str(allocation.id),
                "unknown",
                "unconfirmed",
                reason_code="sandbox_cleanup_unconfirmed",
                environment_id=environment,
            )
        if status != "ok":
            return ExecutionReceipt(
                str(allocation.id),
                status,
                "confirmed",
                reason_code=reason,
                environment_id=environment,
            )
        try:
            context.require_running()
        except StopRequested:
            raise PhaseCanceled(cleanup_confirmed=True) from None
        allocation = await self._begin_phase(self.allocations.phase, accepted, "collect")
        receipt = await self._collect(allocation, context.control.remaining)
        return receipt

    async def _collect(
        self, allocation: Allocation, remaining: float, *, clean: bool = False
    ) -> ExecutionReceipt:
        sandbox = None
        payload = None
        file = None
        code = "sandbox_collection_failed"
        succeeded = False
        try:
            async with asyncio.timeout(remaining):
                sandbox, allocation = await self._create(
                    allocation, read_only=not clean, remaining=remaining
                )
                execution = await sandbox.commands.run(
                    "/usr/local/bin/python /opt/compass/collector.py" + (" clean" if clean else ""),
                    opts=RunCommandOpts(timeout=timedelta(seconds=min(10, remaining))),
                )
                if execution.error or execution.exit_code not in {0, None}:
                    raise ToolRejected(code)
                raw = "".join(entry.text for entry in execution.logs.stdout)
                if len(raw.encode()) > 800 * 1024:
                    raise ToolRejected(code)
                envelope = json.loads(raw)
                if clean:
                    if envelope != {"clean": True}:
                        raise ToolRejected(code)
                else:
                    payload = bounded_payload(envelope["result"])
                    if envelope.get("chart") is not None:
                        file = base64.b64decode(envelope["chart"], validate=True)
                    elif envelope.get("chartRejected"):
                        # Invalid chart does not discard independently validated numeric results.
                        file = b"invalid"
                succeeded = True
        except (Exception, asyncio.CancelledError):
            pass
        finally:
            if sandbox:
                try:
                    await sandbox.close()
                except (Exception, asyncio.CancelledError):
                    pass
            confirmed = await self._finish(allocation, clean=clean and succeeded)
        if confirmed is None:
            return ExecutionReceipt(
                str(allocation.id),
                "unknown",
                "unconfirmed",
                reason_code="sandbox_cleanup_unconfirmed",
            )
        if not succeeded:
            return ExecutionReceipt(str(allocation.id), "error", "confirmed", reason_code=code)
        return ExecutionReceipt(
            str(allocation.id),
            "ok",
            "confirmed",
            payload,
            file=file,
            environment_id=str(allocation.id),
        )

    async def inspect(self, identifier, owner) -> ExecutionReceipt:
        allocation = await asyncio.to_thread(self.allocations.get, identifier, owner)
        allocation = await asyncio.to_thread(self.allocations.phase, allocation, "inspect", owner)
        return await self._collect(allocation, 30)

    async def confirmed_executions(self, identifiers: set[str]) -> list[str]:
        return await asyncio.to_thread(self.allocations.confirmed_executions, identifiers)

    async def reconcile(self) -> list[str]:
        released = []
        for allocation in await asyncio.to_thread(self.allocations.claim_due):
            if allocation.state == "retained":
                try:
                    allocation = await asyncio.to_thread(
                        self.allocations.phase, allocation, "clean"
                    )
                    result = await self._collect(allocation, 30, clean=True)
                    if result.cleanup == "confirmed":
                        released.append(str(allocation.id))
                except ToolRejected:
                    pass
                continue
            manager = await SandboxManager.create(self.cleanup_connection)
            try:
                matches = await manager.list_sandbox_infos(
                    SandboxFilter(metadata=allocation.metadata, page_size=100, page=1)
                )
                if not matches.sandbox_infos:
                    # An unacknowledged create can still arrive late. Without an observed ID,
                    # keep its lease quarantined until reconciliation locates the resource.
                    if allocation.sandbox_id is None or not await self._delete_confirmed(
                        allocation
                    ):
                        continue
                # Delete all resources belonging to the phase before releasing its quota.
                for info in matches.sandbox_infos:
                    from dataclasses import replace

                    if not await self._delete_confirmed(replace(allocation, sandbox_id=info.id)):
                        break
                else:
                    if len(matches.sandbox_infos) < 100:
                        await asyncio.to_thread(self.allocations.confirmed, allocation)
                        released.append(str(allocation.id))
            except Exception:
                pass
            finally:
                await manager.close()
        return released
