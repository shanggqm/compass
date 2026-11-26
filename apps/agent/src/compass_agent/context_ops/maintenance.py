"""Daily Scheduler retention: expired Badcase evidence remains pinned but unreadable."""

from datetime import UTC, datetime

from sqlalchemy import delete, select
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from compass_agent.context_ops.entities import ContextBadcase, ContextSnapshot


def purge_expired(engine, *, now=None):
    with Session(engine) as tx, tx.begin():
        result = tx.execute(
            delete(ContextSnapshot).where(
                ContextSnapshot.expires_at < (now or datetime.now(UTC)),
                ~select(ContextBadcase.id)
                .where(
                    ContextBadcase.snapshot_ref == ContextSnapshot.snapshot_ref,
                )
                .exists(),
            )
        )
        assert isinstance(result, CursorResult)
        return result.rowcount
