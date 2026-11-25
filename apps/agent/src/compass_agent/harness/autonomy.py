"""Request-owned execution limits for the existing ReAct loop."""

from contextlib import contextmanager
from dataclasses import dataclass
from uuid import UUID

from compass_agent.llm.contracts import ExecutionRequest, RuntimeResult
from compass_agent.schemas import RUN_STREAM_LIMITS, RunAutonomyBudget, RunAutonomyUsage
from compass_agent.settings import AutonomyModelProfile
from compass_agent.tools.contracts import ToolBusinessStopped

BUDGET_REASONS = {"max_steps", "max_tool_calls", "token_budget", "cost_budget"}


class AutonomyStopped(ToolBusinessStopped):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True, slots=True)
class AutonomyLimits:
    steps: int = RUN_STREAM_LIMITS["steps"]
    tools: int = RUN_STREAM_LIMITS["steps"] * RUN_STREAM_LIMITS["toolsPerStep"]
    tokens: int = 10_000_000
    cost_microusd: int = 10_000_000

    def __post_init__(self) -> None:
        if not (
            1 <= self.steps <= RUN_STREAM_LIMITS["steps"]
            and 0 <= self.tools <= RUN_STREAM_LIMITS["steps"] * RUN_STREAM_LIMITS["toolsPerStep"]
        ):
            raise ValueError("invalid autonomy step or tool limit")
        if self.tokens < 0 or self.cost_microusd < 0:
            raise ValueError("autonomy usage limits must be nonnegative")


@dataclass(frozen=True, slots=True)
class Reservation:
    profile: AutonomyModelProfile
    output_limit: int

    def cost(self, input_tokens: int, output_tokens: int) -> int:
        return (
            input_tokens * self.profile.input_microusd
            + output_tokens * self.profile.output_microusd
        )


class AutonomyBudget:
    def __init__(self, profiles: tuple[AutonomyModelProfile, ...], limits: AutonomyLimits):
        self.profiles = profiles
        self.limits = limits
        self.steps = self.tools = self.tokens = self.cost_microusd = 0
        self.attempts = self.unknown_attempts = 0
        self.reservation: tuple[UUID, Reservation] | None = None
        self.generation: UUID | None = None
        self.counted_generation: UUID | None = None
        self.reason: str | None = None
        self.closed = False
        self.children: set[AutonomyBudget] = set()
        self.root = self

    def committed(self, resource: str) -> int:
        """Count settled usage and outstanding reservations exactly once in the tree."""
        reserved = 0
        if self.reservation:
            _, reservation = self.reservation
            reserved = (
                reservation.profile.input_tokens + reservation.output_limit
                if resource == "tokens"
                else reservation.cost(reservation.profile.input_tokens, reservation.output_limit)
            )
        return (
            getattr(self, resource)
            + reserved
            + sum(child.committed(resource) for child in self.children)
        )

    def remaining(self, resource: str) -> int:
        return max(
            0,
            min(
                getattr(self.limits, resource) - self.committed(resource),
                getattr(self.root.limits, resource) - self.root.committed(resource),
            ),
        )

    def require_continue(self) -> None:
        if self.reason is not None:
            raise AutonomyStopped(self.reason)

    def require_step(self) -> None:
        self.require_continue()
        if self.steps >= self.limits.steps:
            raise AutonomyStopped("max_steps")

    def start_tool(self) -> None:
        self.require_continue()
        if self.tools >= self.limits.tools:
            raise AutonomyStopped("max_tool_calls")
        self.tools += 1

    def reserve(self, request: ExecutionRequest, attempt_id: UUID) -> bool:
        if (
            self.closed
            or self.root.closed
            or self.reason is not None
            or self.root.reason is not None
            or self.reservation is not None
        ):
            return False
        spec = request.access.spec
        value = request.input
        matches = [
            profile
            for profile in self.profiles
            if (profile.provider_id, profile.binding_version, profile.sdk_model, profile.api_base)
            == (spec.provider_id, spec.binding_version, spec.sdk_model, spec.api_base)
        ]
        decision = value.kind == "decision" and spec.adapter == "systemone_decision"
        output_limit = (
            matches[0].output_tokens if decision and matches else getattr(value, "max_tokens", 0)
        )
        if (
            len(matches) != 1
            or not (decision or (spec.adapter == "litellm_chat" and value.kind == "generate"))
            or (value.kind == "generate" and value.thinking and spec.vendor != "deepseek")
            or request.access.connection_params
            or output_limit > matches[0].output_tokens
        ):
            self.reason = "model_budget_unavailable"
            return False
        profile = matches[0]
        reservation = Reservation(profile, output_limit)
        first_step = self.generation == request.context.call_id != self.counted_generation
        if first_step and self.steps >= self.limits.steps:
            self.reason = "max_steps"
        elif profile.input_tokens + output_limit > self.remaining("tokens"):
            self.reason = "token_budget"
        elif reservation.cost(profile.input_tokens, output_limit) > self.remaining("cost_microusd"):
            self.reason = "cost_budget"
        if self.reason:
            return False
        if first_step:
            self.steps += 1
            self.counted_generation = self.generation
        self.reservation = (attempt_id, reservation)
        self.attempts += 1
        return True

    def settle(self, attempt_id: UUID, result: RuntimeResult | None) -> None:
        if self.reservation is None or self.reservation[0] != attempt_id:
            return
        _, reserved = self.reservation
        self.reservation = None
        if result is not None and not result.upstream_sent and result.cleanup_confirmed:
            return
        usage = result.usage if result is not None else None
        input_tokens = usage.input_tokens if usage else None
        output_tokens = usage.output_tokens if usage else None
        uncertain = (
            result is None or result.upstream_outcome_unknown or not result.cleanup_confirmed
        )
        if uncertain or input_tokens is None or output_tokens is None:
            self.unknown_attempts += 1
        measured_input = (
            max(input_tokens or 0, reserved.profile.input_tokens)
            if uncertain or input_tokens is None
            else input_tokens
        )
        measured_output = (
            max(output_tokens or 0, reserved.output_limit)
            if uncertain or output_tokens is None
            else output_tokens
        )
        if usage and usage.reasoning_tokens is not None:
            measured_output = max(measured_output, usage.reasoning_tokens)
        self.tokens += measured_input + measured_output
        self.cost_microusd += reserved.cost(measured_input, measured_output)
        if (
            measured_input > reserved.profile.input_tokens
            or measured_output > reserved.output_limit
        ):
            self.reason = "usage_bound_violation"

    def can_slice(self, steps):
        return (
            self.reason is None
            and len(self.children) + len(steps) <= 2
            and self.limits.steps - self.steps - sum(c.limits.steps for c in self.children)
            >= sum(steps) + 1
            and self.limits.tools - self.tools - sum(c.limits.tools for c in self.children)
            >= sum(v + 1 for v in steps) + 1
            and self.remaining("tokens") >= len(steps)
            and self.remaining("cost_microusd") >= len(steps)
        )

    @contextmanager
    def child_budget(self, *, steps=3):
        self.require_continue()
        available = AutonomyLimits(
            steps=max(
                1, self.limits.steps - self.steps - sum(c.limits.steps for c in self.children)
            ),
            tools=max(
                0, self.limits.tools - self.tools - sum(c.limits.tools for c in self.children)
            ),
            tokens=self.remaining("tokens"),
            cost_microusd=self.remaining("cost_microusd"),
        )
        if (
            available.steps < 3
            or available.tools < 3
            or available.tokens < 1
            or len(self.children) >= 2
        ):
            raise AutonomyStopped("max_steps" if available.steps < 3 else "max_tool_calls")
        # Money and tokens are reserved at dispatch against the shared Run pool.
        # Fixed thirds can reject both children even when both calls fit together.
        # Step/tool slices remain isolated so children cannot consume the parent loop.
        child = AutonomyBudget(
            self.profiles,
            AutonomyLimits(
                steps=min(steps, available.steps - 1),
                tools=min(steps + 1, available.tools - 1),
                tokens=self.limits.tokens,
                cost_microusd=self.limits.cost_microusd,
            ),
        )
        child.root = self.root
        self.children.add(child)
        try:
            yield child
        finally:
            child.snapshot()
            self.children.remove(child)
            self.steps += child.steps
            self.tools += child.tools
            self.tokens += child.tokens
            self.cost_microusd += child.cost_microusd
            self.attempts += child.attempts
            self.unknown_attempts += child.unknown_attempts
            if child.reason == "usage_bound_violation":
                self.reason = child.reason

    def snapshot(self) -> RunAutonomyBudget:
        self.closed = True
        if self.reservation:
            self.settle(self.reservation[0], None)

        def usage(limit: int, used: int) -> RunAutonomyUsage:
            return RunAutonomyUsage(limit=limit, used=used, remaining=max(0, limit - used))

        return RunAutonomyBudget(
            steps=usage(self.limits.steps, self.steps),
            tools=usage(self.limits.tools, self.tools),
            tokens=usage(self.limits.tokens, self.tokens),
            costMicrousd=usage(self.limits.cost_microusd, self.cost_microusd),
            attempts=self.attempts,
            unknownAttempts=self.unknown_attempts,
        )


class StopPolicy:
    def __init__(self) -> None:
        self.failures = self.without_progress = 0
        self.queries: dict[object, int] = {}

    def require_query(self, key: object) -> None:
        if self.queries.get(key, 0) >= 2:
            raise AutonomyStopped("repeated_query")

    def start_query(self, key: object) -> None:
        self.queries[key] = self.queries.get(key, 0) + 1

    def record(self, *, ok: bool, progress: bool) -> None:
        self.failures = 0 if ok else self.failures + 1
        self.without_progress = 0 if progress else self.without_progress + 1
        if self.failures >= 3:
            raise AutonomyStopped("consecutive_failures")
        if self.without_progress >= 5:
            raise AutonomyStopped("no_progress")
