"""Short transactions under the shared session → run → goal lock order."""

import logging
from datetime import timedelta
from uuid import uuid4, uuid5

from sqlalchemy import String, and_, cast, or_, select

from compass_agent.goal.contracts import FAILURE_LIMIT, NO_PROGRESS_LIMIT, OPEN_STATUSES
from compass_agent.models.errors import ModelError
from compass_agent.runtime.entities import (
    AgentAttempt,
    AgentGoal,
    AgentOutbox,
    AgentRun,
    AgentSession,
    AgentStep,
)
from compass_agent.runtime.store import TERMINAL, now, owner_filter
from compass_agent.schemas import GoalView, SessionSubmit, TrustedContext

logger = logging.getLogger(__name__)


def open_goal(tx, session_id, *, locked=False):
    query = select(AgentGoal).where(
        AgentGoal.session_id == session_id, AgentGoal.status.in_(OPEN_STATUSES)
    )
    return tx.scalar(query.with_for_update() if locked else query)


def latest_run(tx, session_id, *, locked=False):
    query = (
        select(AgentRun)
        .where(AgentRun.session_id == session_id)
        .order_by(AgentRun.ordinal.desc())
        .limit(1)
    )
    return tx.scalar(query.with_for_update() if locked else query)


def transition(goal, status, reason=None):
    goal.status, goal.reason_code, goal.updated_at = status, reason, now()
    goal.ended_at = now() if status in {"completed", "canceled"} else None


def cancel_session_goals(tx, session_id):
    goal = open_goal(tx, session_id, locked=True)
    if goal:
        transition(goal, "canceled", "session_deleted")


def accepted_usage(tx, run_id):
    receipts = tx.scalars(
        select(AgentAttempt.budget_receipt)
        .join(AgentStep, AgentStep.accepted_attempt_id == AgentAttempt.id)
        .where(AgentAttempt.run_id == run_id)
    ).all()
    return {
        name: sum(int(receipt.get(name, 0)) for receipt in receipts)
        for name in ("tokens", "cost_microusd", "attempts")
    }


class GoalController:
    def __init__(self, store):
        self.store = store

    def view(self, tx, goal, context):
        from compass_agent.goal.ledger import Ledger

        rows = Ledger(self.store).visible(tx, goal, context)
        usage = {"tokens": goal.tokens_used, "cost_microusd": goal.cost_used_microusd}
        for run_id in tx.scalars(
            select(AgentRun.id).where(
                AgentRun.session_id == goal.session_id, AgentRun.ordinal > goal.last_settled_ordinal
            )
        ):
            run = tx.get(AgentRun, run_id)
            if (run.config_snapshot.get("goal") or {}).get("goalId") == str(goal.id):
                current = accepted_usage(tx, run_id)
                for key in usage:
                    usage[key] += current[key]
        tokens_pct = usage["tokens"] * 100 / goal.budget["tokens"]
        cost_pct = usage["cost_microusd"] * 100 / goal.budget["costMicrousd"]
        return GoalView.model_validate(
            {
                "goalId": str(goal.id),
                "revision": goal.revision,
                "status": goal.status,
                "reasonCode": goal.reason_code,
                "reason": goal.runtime_state.get("reason"),
                "contract": goal.contract,
                "budget": goal.budget,
                "satisfied": sorted(
                    {
                        a
                        for entry, body in rows
                        if entry.status == "done" and entry.kind == "conclusion"
                        for a in body.get("acceptanceIds", [])
                    }
                ),
                "usage": {
                    "rounds": goal.rounds_started,
                    "tokens": usage["tokens"],
                    "costMicrousd": usage["cost_microusd"],
                    "tokensPct": tokens_pct,
                    "costPct": cost_pct,
                    "overrun": max(tokens_pct, cost_pct) > 100,
                },
                "counts": {
                    "openTodos": sum(e.kind == "todo" and e.status == "open" for e, _ in rows),
                    "conflicts": sum(e.status == "conflict" for e, _ in rows),
                    "stale": sum(e.status == "stale" for e, _ in rows),
                    "reusedCalls": goal.runtime_state.get("reusedCalls", 0),
                },
            }
        ).model_dump(mode="json", by_alias=True)

    def read(self, session_id, context):
        with self.store.transaction() as tx:
            session = tx.scalar(
                select(AgentSession).where(AgentSession.id == session_id, *owner_filter(context))
            )
            if session is None:
                raise ModelError("not_found", 404)
            goal = tx.scalar(
                select(AgentGoal)
                .where(AgentGoal.session_id == session.id)
                .order_by(AgentGoal.created_at.desc())
                .limit(1)
            )
            run = latest_run(tx, session.id)
            return {
                "goal": self.view(tx, goal, context) if goal else None,
                "runId": str(run.id) if run else None,
            }

    def act(self, session_id, command, context):
        from compass_agent.runtime.commands import control_run

        payload = {"sessionId": str(session_id), **command.model_dump(mode="json", by_alias=True)}
        with self.store.transaction(admission=command.action == "resume") as tx:
            old = self.store.command(tx, context, command.request_id, payload)
            session = tx.scalar(
                select(AgentSession)
                .where(AgentSession.id == session_id, *owner_filter(context))
                .with_for_update()
            )
            if session is None:
                raise ModelError("not_found", 404)
            if old:
                return old.response
            run = latest_run(tx, session.id, locked=True)
            goal = open_goal(tx, session.id, locked=True)
            if goal is None:
                raise ModelError("not_found", 404)
            if goal.revision != command.expected_revision:
                raise ModelError("goal_stale_revision", 409)
            action, status = command.action, goal.status
            control = None
            active = run is not None and run.status not in TERMINAL
            if (action in {"pause", "interrupt"} and status == "active") or (
                action == "interrupt" and status == "paused"
            ):
                if (
                    action == "interrupt"
                    and active
                    and run is not None
                    and (run.config_snapshot.get("goal") or {}).get("round") is None
                ):
                    raise ModelError("goal_invalid_transition", 409)
                transition(
                    goal,
                    "paused",
                    (
                        "user_interrupted"
                        if action == "interrupt" and status == "active"
                        else "user_paused"
                    ),
                )
                if active and run is not None:
                    control = "pause" if action == "pause" and run.status == "running" else "cancel"
            elif action == "resume" and status in {"paused", "blocked"}:
                transition(goal, "active")
                goal.runtime_state = {
                    **goal.runtime_state,
                    "failureRounds": 0,
                    "noProgressRounds": 0,
                }
                if active and run is not None:
                    if run.status != "paused":
                        raise ModelError("goal_invalid_transition", 409)
                    control = "resume"
            elif action == "cancel":
                transition(goal, "canceled", "user_canceled")
                control = "cancel" if active else None
            else:
                raise ModelError("goal_invalid_transition", 409)
            # Every user control invalidates stale browser controls and resume checks.
            goal.revision += 1
            if control:
                events = control_run(self.store, tx, session, run, control)
                self.store.outbox(tx, session, run, events)
            result = {
                "goal": self.view(tx, goal, context),
                "runId": str(run.id) if active and run is not None else None,
            }
            self.store.record_command(
                tx,
                context,
                command.request_id,
                "goal_action",
                payload,
                session.id,
                run.id if run else None,
                result,
            )
            return result

    def propose(self, tx, run, proposal):
        goal = open_goal(tx, run.session_id, locked=True)
        if goal:
            if proposal.goal_id != str(goal.id):
                raise ModelError("goal_exists", 409)
            if proposal.expected_revision != goal.revision:
                raise ModelError("goal_stale_revision", 409)
            if goal.status not in {"active", "budget_limited", "paused"}:
                raise ModelError("goal_not_active", 409)
            goal.revision += 1
        else:
            if proposal.goal_id:
                raise ModelError("goal_stale_revision", 409)
            goal = AgentGoal(
                id=uuid4(),
                session_id=run.session_id,
                revision=1,
                rounds_started=0,
                tokens_used=0,
                cost_used_microusd=0,
                ledger_revision=0,
                last_settled_ordinal=run.ordinal - 1,
                runtime_state={},
                created_at=now(),
            )
            tx.add(goal)
        goal.contract = proposal.contract.model_dump(mode="json", by_alias=True)
        goal.budget = proposal.budget.model_dump(mode="json", by_alias=True)
        transition(goal, "active")
        goal.runtime_state = {**goal.runtime_state, "noProgressRounds": 0, "failureRounds": 0}
        run.config_snapshot = {
            **run.config_snapshot,
            "goal": {
                **run.config_snapshot.get("goal", {"round": None}),
                "goalId": str(goal.id),
                "ledgerRevision": goal.ledger_revision,
            },
        }
        tx.flush()
        return goal

    def stop_reason(self, goal):
        rt, budget = goal.runtime_state, goal.budget
        for used, limit, status, reason in (
            (goal.tokens_used, budget["tokens"], "budget_limited", "token_budget"),
            (goal.cost_used_microusd, budget["costMicrousd"], "budget_limited", "cost_budget"),
            (goal.rounds_started, budget["maxRounds"], "budget_limited", "max_rounds"),
            (rt.get("failureRounds", 0), FAILURE_LIMIT, "blocked", "runtime_failures"),
            (rt.get("noProgressRounds", 0), NO_PROGRESS_LIMIT, "blocked", "no_progress"),
        ):
            if used >= limit:
                return status, reason
        return None

    def settle(self, tx, goal, run):
        goal.last_settled_ordinal = run.ordinal
        meta = run.config_snapshot.get("goal") or {}
        if meta.get("goalId") != str(goal.id):
            return
        usage = accepted_usage(tx, run.id)
        goal.tokens_used += usage["tokens"]
        goal.cost_used_microusd += usage["cost_microusd"]
        is_round = meta.get("round") is not None
        if is_round and run.status == "canceled" and run.started_at is None:
            goal.rounds_started = max(0, goal.rounds_started - 1)
            return
        if goal.status == "paused" and goal.reason_code == "user_interrupted":
            if not is_round:
                transition(goal, "active")
            return
        if goal.status != "active":
            return
        if run.reason_code in {"context_access_changed", "forbidden", "access_changed"}:
            transition(goal, "blocked", "access_changed")
            return
        if run.status == "canceled" and run.reason_code != "goal_inactive":
            transition(goal, "paused", "run_canceled")
            return
        goal.runtime_state = {
            **goal.runtime_state,
            "failureRounds": goal.runtime_state.get("failureRounds", 0) + 1
            if run.status == "failed"
            else 0,
            "noProgressRounds": goal.runtime_state.get("noProgressRounds", 0) + 1
            if meta.get("ledgerRevision") == goal.ledger_revision
            else 0,
        }
        if reason := self.stop_reason(goal):
            transition(goal, *reason)

    def process(self, goal_id):
        with self.store.transaction(admission=True) as tx:
            session_id = tx.scalar(select(AgentGoal.session_id).where(AgentGoal.id == goal_id))
            session = tx.scalar(
                select(AgentSession)
                .where(AgentSession.id == session_id)
                .with_for_update(skip_locked=True)
            )
            if session is None:
                return
            run = latest_run(tx, session.id, locked=True)
            goal = tx.scalar(select(AgentGoal).where(AgentGoal.id == goal_id).with_for_update())
            if goal is None:
                return
            goal.updated_at = now()
            if session.deleted_at:
                cancel_session_goals(tx, session.id)
                return
            if (
                goal.status == "active"
                and run is not None
                and run.status == "paused"
                and run.recovery_status == "blocked"
                and (run.config_snapshot.get("goal") or {}).get("goalId") == str(goal.id)
            ):
                transition(goal, "blocked", run.reason_code)
                goal.revision += 1
                goal.runtime_state = {
                    **goal.runtime_state,
                    "reason": "运行恢复受阻，自动执行已停止。运行状态修复后可继续，也可结束目标。",
                }
                return
            if (
                run is None
                or run.status not in TERMINAL
                or run.public_view["run"]["status"] not in TERMINAL
            ):
                return
            if tx.scalar(
                select(AgentOutbox.id)
                .where(AgentOutbox.run_id == run.id, AgentOutbox.delivered_at.is_(None))
                .limit(1)
            ):
                return
            for ended in tx.scalars(
                select(AgentRun)
                .where(
                    AgentRun.session_id == session.id,
                    AgentRun.ordinal > goal.last_settled_ordinal,
                    AgentRun.status.in_(TERMINAL),
                )
                .order_by(AgentRun.ordinal)
            ):
                self.settle(tx, goal, ended)
            if goal.status != "active":
                return
            if reason := self.stop_reason(goal):
                transition(goal, *reason)
                return
            if goal.retry_after and goal.retry_after > now():
                return
            number = goal.rounds_started + 1
            # A canceled queued round can reuse its display number, never its command identity.
            request_id = uuid5(goal.id, f"after:{run.ordinal}:round:{number}")
            previous = run.config_snapshot["command"]
            command = SessionSubmit.model_validate(
                {
                    "requestId": request_id,
                    "sessionId": session.id,
                    "question": f"继续推进目标 · 第 {number} 轮",
                    "selection": previous["selection"],
                    "constraints": previous.get("constraints"),
                }
            )
            context = TrustedContext.model_validate(run.config_snapshot["context"])
            self.store.submit_in_transaction(
                tx,
                command,
                context,
                goal_round=number,
                run_environment=run.config_snapshot.get("promptEnvironment"),
            )
            goal.rounds_started, goal.retry_after = number, None

    def tick(self, limit=16):
        if not (self.store.scheduler or self.store.distributed):
            return
        with self.store.transaction() as tx:
            unsettled = (
                select(AgentRun.id)
                .where(
                    AgentRun.session_id == AgentGoal.session_id,
                    AgentRun.ordinal > AgentGoal.last_settled_ordinal,
                    AgentRun.status.in_(TERMINAL),
                    AgentRun.config_snapshot["goal"]["goalId"].as_string()
                    == cast(AgentGoal.id, String),
                )
                .exists()
            )
            running = (
                select(AgentRun.id)
                .where(
                    AgentRun.session_id == AgentGoal.session_id,
                    AgentRun.status.not_in(TERMINAL),
                )
                .exists()
            )
            blocked = (
                select(AgentRun.id)
                .where(
                    AgentRun.session_id == AgentGoal.session_id,
                    AgentRun.status == "paused",
                    AgentRun.recovery_status == "blocked",
                    AgentRun.config_snapshot["goal"]["goalId"].as_string()
                    == cast(AgentGoal.id, String),
                )
                .exists()
            )
            candidates = list(
                tx.scalars(
                    select(AgentGoal.id)
                    .where(
                        or_(AgentGoal.status == "active", unsettled),
                        or_(~running, and_(AgentGoal.status == "active", blocked)),
                        or_(AgentGoal.retry_after.is_(None), AgentGoal.retry_after <= now()),
                    )
                    .order_by(AgentGoal.updated_at, AgentGoal.id)
                    .limit(limit)
                )
            )
        for goal_id in candidates:
            try:
                self.process(goal_id)
            except ModelError as error:
                if error.status in {429, 404}:
                    with self.store.transaction() as tx:
                        sid = tx.scalar(select(AgentGoal.session_id).where(AgentGoal.id == goal_id))
                        tx.scalar(
                            select(AgentSession).where(AgentSession.id == sid).with_for_update()
                        )
                        goal = tx.get(AgentGoal, goal_id, with_for_update=True)
                        if goal and error.status == 429:
                            goal.retry_after = now() + timedelta(seconds=30)
                        elif goal:
                            transition(goal, "canceled", "session_deleted")
                elif error.status not in {409, 503}:
                    logger.exception("goal.admission_failed")
            except Exception:
                logger.exception("goal.process_failed")
