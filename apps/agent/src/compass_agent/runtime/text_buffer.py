"""Bounded model output with one supervised writer; Redis is outside the callback."""

import asyncio
from collections.abc import Awaitable, Callable
from uuid import UUID, uuid4

from compass_agent.models.errors import ModelError
from compass_agent.schemas import RUN_STREAM_LIMITS


class TextBuffer:
    def __init__(self, write: Callable[[UUID, str], Awaitable[None]], *, interval=0.1):
        self.write = write
        self.interval = interval
        self.pending = ""
        self.bytes = 0
        self.closed = False
        self.wake = asyncio.Event()
        self.task: asyncio.Task | None = None

    def push(self, text: str):
        if self.closed:
            raise ModelError("execution_fenced", 409)
        if self.task and self.task.done():
            self.task.result()
        size = self.bytes + len(text.encode())
        if size > RUN_STREAM_LIMITS["answerBytes"]:
            raise ModelError("run_content_limit", 409)
        self.bytes = size
        self.pending += text
        self.wake.set()
        if self.task is None:
            self.task = asyncio.create_task(self._write())

    async def _write(self):
        while True:
            await self.wake.wait()
            self.wake.clear()
            while self.pending:
                text, self.pending = self.pending[:8192], self.pending[8192:]
                await self.write(uuid4(), text)
                if not self.closed:
                    # Coalesce the next chunk while receiving continues without I/O.
                    try:
                        async with asyncio.timeout(self.interval):
                            while not self.closed:
                                self.wake.clear()
                                await self.wake.wait()
                    except TimeoutError:
                        pass
            if self.closed:
                return

    async def finish(self):
        self.closed = True
        self.wake.set()
        if self.task:
            # Cancellation must not abandon an in-flight database commit in a thread.
            try:
                await asyncio.shield(self.task)
            except asyncio.CancelledError:
                await self.task
                raise

    async def abort(self):
        self.pending = ""
        await self.finish()
