"""A slow writer must not serialize model callbacks or leak a task on cancellation."""

import asyncio

import pytest
from compass_agent.models.errors import ModelError
from compass_agent.runtime.text_buffer import TextBuffer


def test_slow_writer_coalesces_without_blocking_callbacks():
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        writes = []

        async def write(identifier, content):
            entered.set()
            await release.wait()
            writes.append((identifier, content))

        buffer = TextBuffer(write, interval=0.01)
        buffer.push("首块")
        await entered.wait()
        for _ in range(500):
            buffer.push("后续")
        assert not writes
        release.set()
        await buffer.finish()
        assert "".join(content for _, content in writes) == "首块" + "后续" * 500
        assert len(writes) == 2
        assert len({identifier for identifier, _ in writes}) == 2
        assert buffer.task and buffer.task.done()

    asyncio.run(scenario())


def test_writer_failure_and_capacity_are_observed():
    async def scenario():
        async def write(identifier, content):
            raise ModelError("runtime_unavailable", 503)

        buffer = TextBuffer(write)
        buffer.push("正文")
        with pytest.raises(ModelError, match="runtime_unavailable"):
            await buffer.finish()
        buffer = TextBuffer(write)
        with pytest.raises(ModelError, match="run_content_limit"):
            buffer.push("文" * 12000)
        assert buffer.task is None

    asyncio.run(scenario())


def test_cancel_waits_for_inflight_commit_and_discards_only_uncommitted_buffer():
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        written = []

        async def write(identifier, content):
            entered.set()
            await release.wait()
            written.append(content)

        buffer = TextBuffer(write)
        buffer.push("已开始提交")
        await entered.wait()
        buffer.push("仍在内存")
        closing = asyncio.create_task(buffer.abort())
        await asyncio.sleep(0)
        closing.cancel()
        await asyncio.sleep(0)
        assert not closing.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await closing
        assert written == ["已开始提交"]
        assert buffer.task and buffer.task.done()

    asyncio.run(scenario())
