"""Request-local execution ports. Domain modules depend on ports, never the durable store."""

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any
from uuid import uuid4 as random_uuid

# Set only by the authenticated durable execution service, never by an HTTP payload.
current_execution: ContextVar[Any] = ContextVar("durable_execution", default=None)
execution_path: ContextVar[str] = ContextVar("execution_path", default="root")


def uuid4():
    journal = current_execution.get()
    return journal.identifier(execution_path.get()) if journal else random_uuid()


@contextmanager
def execution_scope(name, *, absolute=False):
    token = execution_path.set(name if absolute else f"{execution_path.get()}/{name}")
    try:
        yield
    finally:
        execution_path.reset(token)


async def recorded(
    kind,
    key,
    inputs,
    operation,
    result_type,
    *,
    definition=None,
    budget=None,
    settled_on_return=False,
):
    journal = current_execution.get()
    if journal is None:
        return await operation()
    return await journal.call(
        kind,
        f"{execution_path.get()}/{key}",
        inputs,
        operation,
        result_type,
        definition=definition,
        budget=budget,
        settled_on_return=settled_on_return,
    )
