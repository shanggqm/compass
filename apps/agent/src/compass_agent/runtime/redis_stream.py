"""Bounded, persist-before-observe Redis Streams. Writes are never blindly retried."""

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from uuid import UUID
from weakref import WeakValueDictionary

from redis.asyncio import Redis
from redis.asyncio.retry import Retry
from redis.backoff import NoBackoff
from redis.exceptions import RedisError

from compass_agent.llm.continuation import canonical
from compass_agent.models.errors import ModelError
from compass_agent.runtime.public_projection import stream_position
from compass_agent.runtime.store import TERMINAL
from compass_agent.runtime.stream_publication import (
    RETAIN,
    VALIDATE,
    append_batches,
    publication_keys,
)

PREFIX = "compass:agent:run:"
logger = logging.getLogger(__name__)


def redis_text(value) -> str:
    if not isinstance(value, str):
        raise ModelError("stream_data_invalid", 503)
    return value


class RedisStream:
    def __init__(self, url: str, password: str = ""):
        options = {
            "password": password or None,
            "decode_responses": True,
            "socket_connect_timeout": 2,
            "socket_timeout": 3,
            "retry": Retry(NoBackoff(), 0),
            "max_connections": 16,
        }
        self.writer = Redis.from_url(url, **options)
        self.reader = Redis.from_url(url, **{**options, "retry": Retry(NoBackoff(), 1)})
        self.confirmed: dict[UUID, str] = {}
        self.sizes: dict[UUID, int] = {}

    @staticmethod
    def key(run_id):
        return f"{PREFIX}{run_id}:events"

    async def ready(self):
        try:
            await self.writer.ping()
            config = await self.writer.config_get("appendonly", "appendfsync", "maxmemory-policy")
            info = await self.writer.info("persistence")
            if (
                config
                != {"appendonly": "yes", "appendfsync": "always", "maxmemory-policy": "noeviction"}
                or info.get("aof_last_write_status") != "ok"
            ):
                raise ModelError("redis_durability_unavailable", 503)
        except RedisError:
            raise ModelError("runtime_unavailable", 503) from None

    async def close(self):
        await self.reader.aclose()
        await self.writer.aclose()

    async def rows(self, run_id, after="0-0", *, through="+", limit=8192) -> list[tuple[str, dict]]:
        try:
            values = await self.reader.xrange(
                self.key(run_id), min=f"({after}", max=through, count=limit
            )
            result = []
            for seq, row in values or []:
                if row is None:
                    raise ModelError("stream_data_invalid", 503)
                result.append((redis_text(seq), json.loads(row["entry"])))
            return result
        except (RedisError, ValueError, KeyError):
            raise ModelError("runtime_unavailable", 503) from None

    async def validate(self, run, after="0-0"):
        stream_position(after)
        try:
            key = self.key(run.id)
            tail = await self.reader.xrevrange(key, count=1)
            if not tail:
                if run.public_view["run"]["status"] in TERMINAL and run.projected_seq != "0-0":
                    raise ModelError("history_reload_required", 409)
                if run.projected_seq == "0-0" and run.started_at is None:
                    if after != "0-0":
                        raise ModelError("cursor_invalid", 422)
                    return
                raise ModelError("stream_data_lost", 410)
            if stream_position(redis_text(tail[0][0])) < stream_position(run.projected_seq):
                raise ModelError("stream_data_lost", 410)
            projected = run.projected_seq
            if getattr(run, "publish_initialized", False):
                keys = publication_keys(key)
                integrity = await self.reader.eval(VALIDATE, len(keys), *keys)
                if (
                    integrity != 1
                    or projected != "0-0"
                    and not await self.reader.xrange(key, min=projected, max=projected, count=1)
                ):
                    raise ModelError("stream_data_lost", 410)
            if after != "0-0" and not await self.reader.xrange(key, min=after, max=after, count=1):
                raise ModelError("cursor_invalid", 422)
            self.confirmed[run.id] = redis_text(tail[0][0])
        except RedisError:
            raise ModelError("runtime_unavailable", 503) from None

    async def append(self, run, packet, *, outbox=None, index=None, count=None):
        await self.validate(run)
        entry = {"packet": packet}
        if outbox is not None:
            entry.update(outboxId=str(outbox), batchIndex=index, batchSize=count)
        encoded = canonical(entry).decode()
        # A lost XADD reply is resolved by original packet identity and byte-equivalent content.
        existing = await self.rows(run.id)
        for seq, row in existing:
            if row["packet"]["packetId"] == packet["packetId"]:
                if row != entry:
                    raise ModelError("packet_conflict", 409)
                self.confirmed[run.id] = (
                    seq
                    if stream_position(seq) > stream_position(self.confirmed.get(run.id, "0-0"))
                    else self.confirmed[run.id]
                )
                return seq
        used = sum(len(canonical(row)) for _, row in existing)
        limit = 4 * 1024 * 1024 if outbox else 3584 * 1024
        if used + len(encoded.encode()) > limit:
            raise ModelError("run_content_limit", 409)
        try:
            seq = redis_text(await self.writer.xadd(self.key(run.id), {"entry": encoded}))
        except RedisError:
            # One read-only reconciliation; callers retain the same packet/batch for later repair.
            for seq, row in await self.rows(run.id):
                if row == entry:
                    self.confirmed[run.id] = seq
                    return seq
            raise ModelError("runtime_unavailable", 503) from None
        self.confirmed[run.id] = seq
        self.sizes[run.id] = used + len(encoded.encode())
        return seq

    async def retain_terminal(self, run, pending):
        if pending or run.public_view["run"]["status"] not in TERMINAL:
            return
        rows = await self.reader.xrevrange(self.key(run.id), count=1)
        if rows and rows[0][0] == run.projected_seq:
            if await self.writer.ttl(self.key(run.id)) == -1:
                await self.writer.expire(self.key(run.id), 3600)


def complete_prefix(rows):
    result, batch = [], []
    for seq, row in rows:
        if not row.get("outboxId"):
            if batch:
                break
            result.append((seq, row))
            continue
        if not batch and row["batchIndex"] != 0:
            raise ModelError("stream_batch_invalid", 410)
        if batch and (
            row["outboxId"] != batch[0][1]["outboxId"] or row["batchIndex"] != len(batch)
        ):
            raise ModelError("stream_batch_invalid", 410)
        batch.append((seq, row))
        if len(batch) == row["batchSize"]:
            result.extend(batch)
            batch = []
    return result


class OutboxPublisher:
    def __init__(self, store, streams):
        self.store, self.streams = store, streams
        self.locks: WeakValueDictionary[UUID, asyncio.Lock] = WeakValueDictionary()
        self.last_projected: dict[UUID, float] = {}
        self.dirty: set[UUID] = set()
        self.unprojected_bytes: dict[UUID, int] = {}
        self.require_owner = lambda: None
        self.concurrency = asyncio.Semaphore(8)
        self.fault: Callable[[str, UUID], Awaitable[None]] | None = None
        self.requested: set[UUID] = set()
        self.background: dict[UUID, asyncio.Task] = {}

    def notify(self, run_id):
        """Coalesce wakeups; committed outboxes are also retried by the existing ticker."""
        self.requested.add(run_id)
        if run_id not in self.background:
            self.background[run_id] = asyncio.create_task(self._publish_requested(run_id))

    async def _publish_requested(self, run_id):
        try:
            while run_id in self.requested:
                self.requested.discard(run_id)
                await self.flush(run_id)
        except Exception as error:
            logger.warning(
                "runtime.publication_pending run_id=%s reason=%s",
                run_id,
                error.code if isinstance(error, ModelError) else type(error).__name__,
            )
        finally:
            self.requested.discard(run_id)
            self.background.pop(run_id, None)

    async def close(self):
        tasks = tuple(self.background.values())
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def channel(self, run_id):
        return self.locks.setdefault(run_id, asyncio.Lock())

    async def flush(self, run_id):
        if self.store.distributed:
            async with self.concurrency:
                await self.flush_shared(run_id)
            return
        async with self.channel(run_id):
            self.require_owner()
            run = await asyncio.to_thread(self.store.read_run, run_id)
            pending = await asyncio.to_thread(self.store.pending, run_id)
            for box in pending:
                for index, packet in enumerate(box.payload["packets"]):
                    await self.streams.append(
                        run, packet, outbox=box.id, index=index, count=len(box.payload["packets"])
                    )
                await self._project(run_id)
                run = await asyncio.to_thread(self.store.read_run, run_id)
            await self._project(run_id)
            run = await asyncio.to_thread(self.store.read_run, run_id)
            await self.streams.retain_terminal(
                run, await asyncio.to_thread(self.store.pending, run_id)
            )

    async def flush_shared(self, run_id):
        self.require_owner()
        if not await asyncio.to_thread(self.store.pending, run_id, limit=1):
            return
        lease = await asyncio.to_thread(self.store.publication, run_id)
        if lease is None:
            return
        try:
            # Re-read under the publish lease so no prior publisher can hide a partially sent batch.
            pending = await asyncio.to_thread(self.store.pending, run_id, limit=32)
            if not pending:
                return
            await self.streams.validate(lease)
            keys = publication_keys(self.streams.key(run_id))
            await append_batches(self.streams.writer, keys, lease, pending)
            if self.fault:
                await self.fault("outbox_appended", run_id)
            rows = complete_prefix(await self.streams.rows(run_id, lease.projected_seq))
            await asyncio.to_thread(self.store.project, run_id, rows, lease)
            run = await asyncio.to_thread(self.store.read_run, run_id)
            if run.status in TERMINAL and not await asyncio.to_thread(
                self.store.pending, run_id, limit=1
            ):
                await self.streams.writer.eval(
                    RETAIN, len(keys), *keys, str(lease.publish_generation), run.projected_seq
                )
        except RedisError:
            raise ModelError("runtime_unavailable", 503) from None
        except ModelError as error:
            if error.code in {"stream_data_lost", "stream_batch_invalid", "packet_conflict"}:
                await asyncio.to_thread(self.store.blocked, run_id, error.code)
            if error.code != "publish_lease_lost":
                raise
        finally:
            await asyncio.to_thread(self.store.end_publication, lease)

    async def _project(self, run_id):
        run = await asyncio.to_thread(self.store.read_run, run_id)
        rows = complete_prefix(await self.streams.rows(run_id, run.projected_seq))
        await asyncio.to_thread(self.store.project, run_id, rows)
        self.last_projected[run_id] = asyncio.get_running_loop().time()
        self.dirty.discard(run_id)
        self.unprojected_bytes[run_id] = 0

    async def delta(self, run_id, packet):
        if self.store.distributed:
            raise ModelError("stream_write_requires_outbox", 409)
        if await asyncio.to_thread(self.store.pending, run_id):
            await self.flush(run_id)
        async with self.channel(run_id):
            self.require_owner()
            run = await asyncio.to_thread(self.store.read_run, run_id)
            await self.streams.append(run, packet)
            self.dirty.add(run_id)
            self.unprojected_bytes[run_id] = self.unprojected_bytes.get(run_id, 0) + len(
                canonical(packet)
            )
            if (
                self.unprojected_bytes[run_id] >= 65536
                or asyncio.get_running_loop().time() - self.last_projected.get(run_id, 0) >= 0.5
            ):
                await self._project(run_id)

    async def tick(self):
        if self.store.distributed:
            identifiers = await asyncio.to_thread(self.store.pending_runs)
            await asyncio.gather(
                *(self.flush(run_id) for run_id in identifiers), return_exceptions=True
            )
            return
        identifiers = {
            box.run_id for box in await asyncio.to_thread(self.store.pending)
        } | self.dirty
        for run_id in identifiers:
            await self.flush(run_id)
