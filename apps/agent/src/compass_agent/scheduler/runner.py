"""Supervise a single live Scheduler role with independent loop deadlines."""

import asyncio
import logging
from uuid import uuid4

from sqlalchemy.exc import SQLAlchemyError

from compass_agent.models.errors import ModelError
from compass_agent.scheduler.lease import HEARTBEAT_SECONDS
from compass_agent.scheduler.repository import MAX_INFLIGHT

logger = logging.getLogger(__name__)


class SchedulerRunner:
    def __init__(self, leases, repository, client):
        self.leases, self.repository, self.client = leases, repository, client
        self.boot_id = uuid4()

    async def send(self, lease, claim):
        result = await self.client.deliver(claim)
        if result.state == "retry":
            await asyncio.to_thread(
                self.repository.failure,
                lease,
                claim,
                result.reason,
                result.delay,
                permanent=result.permanent,
            )
        logger.info(
            "schedule.delivery trigger_id=%s attempt=%s reconciliation=%s state=%s reason=%s",
            claim.trigger_id,
            claim.attempt,
            claim.reconciliation,
            result.state,
            result.reason,
        )

    async def dispatch(self, lease):
        claims = await asyncio.to_thread(
            self.repository.claim, lease, reconciliation=True, limit=MAX_INFLIGHT
        )
        remaining = MAX_INFLIGHT - len(claims)
        if remaining:
            claims.extend(
                await asyncio.to_thread(
                    self.repository.claim, lease, reconciliation=False, limit=remaining
                )
            )
        await asyncio.gather(*(self.send(lease, claim) for claim in claims))

    async def scan(self, lease):
        count = await asyncio.to_thread(self.repository.scan, lease)
        if count:
            logger.info("schedule.scanned plans=%s generation=%s", count, lease.generation)

    async def synchronize(self, lease):
        count = await asyncio.to_thread(self.repository.sync, lease)
        if count:
            logger.info("schedule.synced triggers=%s generation=%s", count, lease.generation)

    async def observe_queue(self, lease):
        count, age = await asyncio.to_thread(self.repository.waiting, lease)
        logger.info("schedule.queue pending=%d oldest_ms=%d", count, age * 1000)

    async def purge_context_snapshots(self):
        from compass_agent.context_ops.maintenance import purge_expired

        await asyncio.to_thread(purge_expired, self.repository.engine)

    @staticmethod
    async def until(stop, interval, operation):
        while not stop.is_set():
            try:
                await operation()
            except SQLAlchemyError:
                logger.warning("scheduler.database_unavailable")
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
            except TimeoutError:
                pass

    async def run(self, stop):
        lease = await asyncio.to_thread(self.leases.acquire, self.boot_id)
        if lease is None:
            logger.warning("scheduler.lease_busy")
            return 2

        async def renew():
            await asyncio.sleep(HEARTBEAT_SECONDS)
            while not stop.is_set():
                await asyncio.to_thread(self.leases.renew, lease)
                try:
                    await asyncio.wait_for(stop.wait(), timeout=HEARTBEAT_SECONDS)
                except TimeoutError:
                    pass

        logger.info("scheduler.ready boot_id=%s generation=%s", lease.boot_id, lease.generation)
        tasks = [
            asyncio.create_task(renew()),
            asyncio.create_task(self.until(stop, 0.5, lambda: self.dispatch(lease))),
            asyncio.create_task(self.until(stop, 1, lambda: self.scan(lease))),
            asyncio.create_task(self.until(stop, 1, lambda: self.synchronize(lease))),
            asyncio.create_task(self.until(stop, 15, lambda: self.observe_queue(lease))),
            asyncio.create_task(self.until(stop, 86400, self.purge_context_snapshots)),
        ]
        stop_wait = asyncio.create_task(stop.wait())
        exit_code = 0
        try:
            done, _ = await asyncio.wait([stop_wait, *tasks], return_when=asyncio.FIRST_COMPLETED)
            for task in done - {stop_wait}:
                error = task.exception()
                if error:
                    code = error.code if isinstance(error, ModelError) else type(error).__name__
                    logger.warning("scheduler.stopped reason=%s", code)
                    exit_code = 2
            stop.set()
            await asyncio.wait(tasks, timeout=5)
        finally:
            for task in [stop_wait, *tasks]:
                if not task.done():
                    task.cancel()
            await asyncio.gather(stop_wait, *tasks, return_exceptions=True)
            try:
                await asyncio.to_thread(self.leases.release, lease)
            except (ModelError, SQLAlchemyError):
                logger.warning("scheduler.lease_release_unavailable")
        return exit_code
