"""Model operations share the Run's epoch and durable private receipt boundary."""

from compass_agent.context_runtime.source_access import read_private, save_private
from compass_agent.execution_context import execution_path
from compass_agent.goal.contracts import BlockGoal, CompleteGoal, ProposeGoal, RecordProgress
from compass_agent.goal.controller import GoalController, transition
from compass_agent.goal.gate import complete
from compass_agent.goal.ledger import Ledger, bound_goal
from compass_agent.models.errors import ModelError
from compass_agent.runtime.private_state import digest


class GoalTools:
    def __init__(self, journal):
        self.journal, self.store = journal, journal.store

    def execute(self, args, context):
        journal = self.journal
        key = f"goal/operation/{execution_path.get()}/{context.step_id}/{context.call_id}"
        with self.store.transaction() as tx:
            _, run = self.store.lock(tx, journal.run_id, context.trusted)
            self.store.require_epoch(run, journal.epoch)
            previous = read_private(self.store, tx, run.id, key)
            if previous:
                if previous["digest"] != digest(args.model_dump(mode="json", by_alias=True)):
                    raise ModelError("request_conflict", 409)
                return previous["result"]
            controller = GoalController(self.store)
            extra = {}
            if isinstance(args, ProposeGoal):
                goal = controller.propose(tx, run, args)
            else:
                goal = bound_goal(tx, run)
                if goal is None or goal.status != "active":
                    raise ModelError("goal_not_active", 409)
                if isinstance(args, RecordProgress):
                    Ledger(self.store).record(tx, goal, args.items, journal, context.trusted)
                elif isinstance(args, CompleteGoal):
                    extra = complete(
                        self.store,
                        tx,
                        goal,
                        args.expected_revision,
                        context.trusted,
                        epoch=journal.epoch,
                    )
                elif isinstance(args, BlockGoal):
                    transition(goal, "blocked", "needs_user_decision")
                    goal.runtime_state = {**goal.runtime_state, "reason": args.reason}
            result = {**extra, "goal": controller.view(tx, goal, context.trusted)}
            journal.goal_id = str(goal.id)
            save_private(
                self.store,
                tx,
                run,
                key,
                {"digest": digest(args.model_dump(mode="json", by_alias=True)), "result": result},
                [],
            )
            return result
