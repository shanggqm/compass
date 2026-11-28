"""Real PostgreSQL + OpenSandbox/NFS; dedicated test NAS and database are required."""

import asyncio
import os
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from compass_agent.tools.contracts import AuthorizedInput, SandboxBinding, ToolRejected
from compass_agent.tools.sandbox.allocations import Allocations
from compass_agent.tools.sandbox.entities import SandboxAllocation, SandboxExecution
from compass_agent.tools.sandbox.shared import SandboxBackend
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from .test_tool_dispatcher import context


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(scope="module")
def engine():
    target = os.environ["AGENT_TEST_DATABASE_URL"]
    assert "/compass_agent_test_" in target
    assert os.environ["COMPASS_SANDBOX_TEST_VOLUME_PREFIX"] != "compass-sandbox-slot"
    value = create_engine(target)
    yield value
    value.dispose()


def backend(engine, idle=1):
    return SandboxBackend(
        Allocations(
            engine,
            os.environ["COMPASS_SANDBOX_TEST_HOST"],
            slots=int(os.environ["COMPASS_SANDBOX_TEST_SLOTS"]),
            retention_s=1,
        ),
        domain=os.environ["COMPASS_SANDBOX_TEST_DOMAIN"],
        api_key=os.environ["COMPASS_SANDBOX_API_KEY"],
        image="compass-sandbox-python:lesson18",
        volume_prefix=os.environ["COMPASS_SANDBOX_TEST_VOLUME_PREFIX"],
        idle_s=idle,
    )


async def authorized(_):
    return None


def ctx(trusted=None):
    value = (
        context("python_analysis")
        if trusted is None
        else context("python_analysis", trusted=trusted)
    )
    return replace(
        value,
        call_id=str(uuid4()),
        sandbox_access=authorized,
        sources={
            "input": AuthorizedInput(value.run_id, value.owner, {"columns": ["v"], "rows": [[7]]})
        },
    )


async def run(service, execution, code):
    return await service.execute(
        SandboxBinding(), {"source_query_id": "input", "code": code}, execution
    )


async def drain(service, engine):
    await asyncio.sleep(1.1)
    active = []
    until = time.monotonic() + 120
    while time.monotonic() < until:
        await service.reconcile()
        with Session(engine) as tx:
            active = list(
                tx.scalars(
                    select(SandboxAllocation).where(
                        SandboxAllocation.host == service.allocations.host,
                        SandboxAllocation.state != "released",
                    )
                )
            )
        if not active:
            return
        await asyncio.sleep(1)
    assert not active, [(a.id, a.state, a.resource_phase) for a in active]


@pytest.mark.anyio
async def test_two_workers_create_once_share_files_and_execute_in_parallel(engine):
    first, second = backend(engine, idle=5), backend(engine, idle=5)
    a, b = ctx(), ctx()
    code = """import time, pathlib, pandas as pd
start=time.time()
pathlib.Path(shared_dir, 'shared.csv').write_text('v\\n7\\n')
time.sleep(1)
result={'start':start,'end':time.time(),'value':int(pd.DataFrame(data['rows']).iloc[0,0])}
"""
    one, two = await asyncio.gather(run(first, a, code), run(second, b, code))
    assert one.status == two.status == "ok", (one, two)
    assert one.environment_id == two.environment_id
    assert max(one.payload["start"], two.payload["start"]) < min(
        one.payload["end"], two.payload["end"]
    )
    chart_code = """import pandas as pd, matplotlib.pyplot as plt, time
start=time.time()
v=int(pd.read_csv(shared_dir+'/shared.csv').v.iloc[0])
plt.plot([0,1],[0,VALUE]);plt.savefig(chart_path)
time.sleep(1)
result={'value':v,'marker':VALUE,'start':start,'end':time.time()}
"""
    one, two = await asyncio.gather(
        run(first, ctx(), chart_code.replace("VALUE", "3")),
        run(second, ctx(), chart_code.replace("VALUE", "9")),
    )
    assert one.status == two.status == "ok", (one, two)
    assert one.environment_id == two.environment_id
    assert one.payload["marker"] == 3 and two.payload["marker"] == 9
    assert one.payload["value"] == two.payload["value"] == 7
    assert one.file and two.file and one.file != two.file
    with Session(engine) as tx:
        environments = list(
            tx.scalars(select(SandboxAllocation).where(SandboxAllocation.shared.is_(True)))
        )
    assert len(environments) == 1
    await asyncio.sleep(5)
    await drain(first, engine)


@pytest.mark.anyio
async def test_cancel_one_command_keeps_companion_and_workspace(engine):
    service, other = backend(engine, idle=5), backend(engine, idle=5)
    a, b = ctx(), ctx()
    first = asyncio.create_task(
        run(
            service,
            a,
            "import time,subprocess,sys,pathlib\n"
            'child="import time,pathlib;time.sleep(7);pathlib.Path("\n'
            "child+=repr(shared_dir+'/late-child')+\").write_text('leaked')\"\n"
            "subprocess.Popen([sys.executable,'-c',child])\n"
            "time.sleep(15)\nresult={'a':1}",
        )
    )
    second = asyncio.create_task(
        run(
            other,
            b,
            "import time,pathlib\ntime.sleep(4)\n"
            "pathlib.Path(shared_dir,'survived').write_text('yes')\nresult={'b':2}",
        )
    )
    count = 0
    for _ in range(150):
        with Session(engine) as tx:
            count = len(
                list(
                    tx.scalars(select(SandboxExecution).where(SandboxExecution.state == "running"))
                )
            )
        if count == 2:
            break
        await asyncio.sleep(0.1)
    assert count == 2
    first.cancel()
    canceled, completed = await asyncio.gather(first, second)
    assert canceled.status == "canceled" and canceled.cleanup == "confirmed", canceled
    assert completed.status == "ok", completed
    await asyncio.sleep(4)
    reused = await run(
        service,
        ctx(),
        "from pathlib import Path\n"
        "assert not Path(shared_dir,'late-child').exists()\n"
        "result={'value':Path(shared_dir,'survived').read_text()}",
    )
    assert reused.status == "ok" and reused.payload == {"value": "yes"}
    assert reused.environment_id == completed.environment_id == canceled.environment_id
    await asyncio.sleep(5)
    await drain(service, engine)


@pytest.mark.anyio
async def test_owner_dimensions_are_separate_and_idle_reclaims(engine):
    service = backend(engine)
    a = ctx()
    result = await run(
        service,
        a,
        "from pathlib import Path\nPath(shared_dir,'secret').write_text('private')\n"
        "result={'saved':True}",
    )
    assert result.status == "ok", result
    values = []
    for dimension in ("user", "tenant", "workspace"):
        if dimension == "user":
            trusted = a.trusted.model_copy(update={"actor_user_id": uuid4()})
        else:
            scope = a.trusted.scope.model_copy(update={dimension + "_id": uuid4()})
            trusted = a.trusted.model_copy(update={"scope": scope})
        values.append(
            await run(
                service,
                ctx(trusted),
                "from pathlib import Path\nresult={'exists':Path(shared_dir,'secret').exists()}",
            )
        )
    assert all(v.status == "ok" and v.payload == {"exists": False} for v in values), values
    assert len({result.environment_id, *[v.environment_id for v in values]}) == 4
    await drain(service, engine)
    again = await run(
        service,
        ctx(),
        "from pathlib import Path\nresult={'exists':Path(shared_dir,'secret').exists()}",
    )
    assert again.status == "ok" and again.payload == {"exists": False}
    assert again.environment_id != result.environment_id
    await drain(service, engine)


@pytest.mark.anyio
async def test_reauthorization_denial_prevents_shared_file_read(engine):
    service = backend(engine)
    saved = await run(
        service,
        ctx(),
        "from pathlib import Path\nPath(shared_dir,'private').write_text('private')\nresult=1",
    )
    assert saved.status == "ok"

    async def denied(runs):
        assert len(runs) == 2
        raise ToolRejected("sandbox_source_stale")

    failed = await run(
        service, replace(ctx(), sandbox_access=denied), "raise AssertionError('must not run')"
    )
    assert failed.status == "error" and failed.reason_code == "sandbox_source_stale"
    assert failed.cleanup == "confirmed"
    await drain(service, engine)


@pytest.mark.anyio
async def test_unknown_legacy_create_converges_after_two_confirmed_absences(engine):
    service = backend(engine)
    value = service.allocations.reserve(
        (uuid4(), uuid4(), uuid4()), uuid4(), uuid4(), "unknown", uuid4(), "digest"
    )
    with Session(engine) as tx, tx.begin():
        row = tx.get(SandboxAllocation, value.id)
        assert row is not None
        row.state = "unknown"
        row.created_at = datetime.now(UTC) - timedelta(hours=1)
        row.resource_deadline = datetime.now(UTC) - timedelta(minutes=1)
    await service.reconcile()
    with Session(engine) as tx, tx.begin():
        row = tx.get(SandboxAllocation, value.id)
        assert row is not None
        assert row.state == "unknown" and row.absent_since
        row.absent_since -= timedelta(seconds=61)
        row.reconcile_until = None
    until = time.monotonic() + 120
    while time.monotonic() < until:
        await service.reconcile()
        with Session(engine) as tx:
            row = tx.get(SandboxAllocation, value.id)
            assert row is not None
            if row.state in {"retained", "released"}:
                break
        await asyncio.sleep(1)
    assert row.state in {"retained", "released"}
    await drain(service, engine)


@pytest.mark.anyio
async def test_remote_create_response_loss_is_found_and_reclaimed(engine, monkeypatch):
    service = backend(engine)
    actual_attach = service.allocations.attach

    def lost_attach(allocation, identifier):
        raise TimeoutError("lost acknowledgment before persistence")

    monkeypatch.setattr(service.allocations, "attach", lost_attach)
    receipt = await run(service, ctx(), "result=1")
    assert receipt.status == "unknown" and receipt.cleanup == "unconfirmed"
    monkeypatch.setattr(service.allocations, "attach", actual_attach)
    await drain(service, engine)
    assert receipt.execution_id in await service.confirmed_executions({receipt.execution_id})


@pytest.mark.anyio
async def test_reconciler_keeps_live_commands_and_recovers_a_dead_worker(engine):
    import subprocess
    import sys
    import tempfile
    from pathlib import Path

    service = backend(engine, idle=5)
    # The child process has its own backend and connection pool, like a second Agent replica.
    script = """import asyncio, os, sys
sys.path.insert(0, "apps/agent")
from sqlalchemy import create_engine
from tests.test_shared_sandbox_integration import backend,ctx,run
async def main():
 await run(backend(create_engine(os.environ['AGENT_TEST_DATABASE_URL']),idle=5),ctx(),
    "import time,pathlib\\ntime.sleep(4)\\n"
    "pathlib.Path(shared_dir,'worker-result').write_text('done')\\nresult=17")
asyncio.run(main())
"""
    with tempfile.TemporaryDirectory() as directory:
        log = Path(directory) / "worker.log"
        with log.open("wb") as output:
            process = subprocess.Popen(
                [sys.executable, "-c", script], stdout=output, stderr=subprocess.STDOUT
            )
        try:
            call = None
            for _ in range(200):
                with Session(engine) as tx:
                    call = tx.scalar(
                        select(SandboxExecution).where(SandboxExecution.state == "running")
                    )
                if call:
                    break
                assert process.poll() is None, log.read_text()
                await asyncio.sleep(0.1)
            assert call is not None
            process.kill()
            process.wait(5)
            # An overdue idle timestamp cannot reclaim an admitted command.
            with Session(engine) as tx, tx.begin():
                allocation = tx.get(SandboxAllocation, call.allocation_id)
                assert allocation is not None
                allocation.idle_until = datetime.now(UTC) - timedelta(seconds=1)
            await service.reconcile()
            with Session(engine) as tx:
                allocation = tx.get(SandboxAllocation, call.allocation_id)
                assert allocation is not None and allocation.state == "running"
                remote = allocation.sandbox_id
            await asyncio.sleep(max(0, (call.deadline - datetime.now(UTC)).total_seconds()) + 0.2)
            await service.reconcile()
            assert str(call.id) in await service.confirmed_executions({str(call.id)})
            reused = await run(
                service,
                ctx(),
                "from pathlib import Path\n"
                "result={'v':Path(shared_dir,'worker-result').read_text()}",
            )
            assert reused.status == "ok" and reused.payload == {"v": "done"}, reused
            assert reused.environment_id == remote
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(5)
    await asyncio.sleep(5)
    await drain(service, engine)


@pytest.mark.anyio
async def test_revocation_during_execution_discards_collected_payload(engine):
    service = backend(engine)
    checks = []

    async def revoked(runs):
        checks.append(runs)
        if len(checks) == 2:
            raise ToolRejected("source_access_denied")

    receipt = await run(
        service,
        replace(ctx(), sandbox_access=revoked),
        "import matplotlib.pyplot as plt\nplt.plot([1,2]);plt.savefig(chart_path)\n"
        "result={'private':7}",
    )
    assert len(checks) == 2
    assert receipt.status == "error" and receipt.reason_code == "source_access_denied"
    assert receipt.payload is None and receipt.file is None and receipt.cleanup == "confirmed"
    await drain(service, engine)


@pytest.mark.anyio
async def test_completed_command_does_not_leave_background_writers(engine):
    service = backend(engine, idle=15)
    code = """import subprocess,sys
child="import time,pathlib;time.sleep(5);pathlib.Path("
child+=repr(shared_dir+'/orphan')+").write_text('bad')"
subprocess.Popen([sys.executable,'-c',child])
result={'done':True}
"""
    receipt = await run(service, ctx(), code)
    assert receipt.status == "ok", receipt
    await asyncio.sleep(6)
    checked = await run(
        service,
        ctx(),
        "from pathlib import Path\nresult={'orphan':Path(shared_dir,'orphan').exists()}",
    )
    assert checked.status == "ok" and checked.payload == {"orphan": False}, checked
    await asyncio.sleep(15)
    await drain(service, engine)
