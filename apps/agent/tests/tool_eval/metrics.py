"""Six primary metrics and four guardrails, with explicit missing-data semantics."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from statistics import mean
from typing import Literal

EvalGroup = Literal["A", "B", "C"]


@dataclass(frozen=True, slots=True)
class CaseEvalRecord:
    case_id: str
    completed: bool
    expected_primary_tools: frozenset[str]
    forbidden_tools: frozenset[str]
    called_tools: tuple[str, ...]
    valid_argument_calls: int
    total_calls: int
    first_turn_succeeded: bool
    redundant_calls: int
    recoverable_errors: int
    recovered_errors: int
    declaration_tokens_sum: int | None
    prompt_tokens_sum: int | None
    clarifications: int
    elapsed_ms: int
    cost_microusd: int | None

    def __post_init__(self):
        counts = (
            self.valid_argument_calls,
            self.total_calls,
            self.redundant_calls,
            self.recoverable_errors,
            self.recovered_errors,
            self.clarifications,
            self.elapsed_ms,
        )
        if any(n < 0 for n in counts):
            raise ValueError("counts must be nonnegative")
        if self.total_calls != len(self.called_tools):
            raise ValueError("total_calls must match called_tools")
        if max(self.valid_argument_calls, self.redundant_calls) > self.total_calls:
            raise ValueError("call numerator exceeds denominator")
        if self.recovered_errors > self.recoverable_errors:
            raise ValueError("recovered errors exceed recoverable errors")
        for n in (self.declaration_tokens_sum, self.prompt_tokens_sum, self.cost_microusd):
            if n is not None and n < 0:
                raise ValueError("measurements must be nonnegative or missing")
        if (self.declaration_tokens_sum is None) != (self.prompt_tokens_sum is None):
            raise ValueError("token numerator and denominator require the same coverage")
        if (
            self.declaration_tokens_sum is not None
            and self.prompt_tokens_sum is not None
            and self.declaration_tokens_sum > self.prompt_tokens_sum
        ):
            raise ValueError("declaration tokens exceed prompt tokens")

    def to_dict(self) -> dict:
        result = asdict(self)
        result["expected_primary_tools"] = sorted(self.expected_primary_tools)
        result["forbidden_tools"] = sorted(self.forbidden_tools)
        return result

    @classmethod
    def from_dict(cls, value: dict) -> CaseEvalRecord:
        fields = dict(value)
        fields["expected_primary_tools"] = frozenset(fields["expected_primary_tools"])
        fields["forbidden_tools"] = frozenset(fields["forbidden_tools"])
        fields["called_tools"] = tuple(fields["called_tools"])
        return cls(**fields)


def _ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def summarize_group(group: EvalGroup, rows: list[CaseEvalRecord]) -> dict:
    """Micro-average call/error/token metrics; macro-average case outcomes.

    Unobserved cost/token coverage keeps the aggregate null. A zero denominator
    means not applicable, including runs with no recoverable error. The runner
    supplies byte/4 estimates for BOTH token operands and labels that basis.
    """
    if not rows:
        raise ValueError("at least one case is required")
    if len({row.case_id for row in rows}) != len(rows):
        raise ValueError("duplicate case IDs; summarize repetitions separately")
    count = len(rows)
    calls = sum(row.total_calls for row in rows)
    errors = sum(row.recoverable_errors for row in rows)
    token_rows = [row for row in rows if row.prompt_tokens_sum is not None]
    cost_rows = [row for row in rows if row.cost_microusd is not None]
    token_share = None
    if len(token_rows) == count:
        token_share = _ratio(
            sum(row.declaration_tokens_sum or 0 for row in token_rows),
            sum(row.prompt_tokens_sum or 0 for row in token_rows),
        )
    selected = sum(
        row.expected_primary_tools.issubset(row.called_tools)
        and row.forbidden_tools.isdisjoint(row.called_tools)
        and (bool(row.expected_primary_tools) or row.completed)
        for row in rows
    )
    return {
        "group": group,
        "case_count": count,
        "primary": {
            "tool_selection_accuracy": selected / count,
            "argument_validity_rate": _ratio(sum(r.valid_argument_calls for r in rows), calls),
            "first_turn_success_rate": sum(r.first_turn_succeeded for r in rows) / count,
            "redundant_call_rate": _ratio(sum(r.redundant_calls for r in rows), calls),
            "error_recovery_rate": _ratio(sum(r.recovered_errors for r in rows), errors),
            "tool_declaration_token_share": token_share,
        },
        "guardrails": {
            "task_completion_rate": sum(r.completed for r in rows) / count,
            "avg_clarifications": mean(r.clarifications for r in rows),
            "avg_elapsed_ms": mean(r.elapsed_ms for r in rows),
            "avg_cost_microusd": (
                mean(r.cost_microusd or 0 for r in cost_rows) if len(cost_rows) == count else None
            ),
        },
        "denominators": {
            "calls": calls,
            "recoverable_errors": errors,
            "token_coverage_cases": len(token_rows),
            "cost_coverage_cases": len(cost_rows),
            "prompt_tokens": sum(r.prompt_tokens_sum or 0 for r in token_rows),
        },
        "token_basis": "ceil(UTF-8 JSON bytes / 4), same estimator for tools and full prompt",
    }


@dataclass(slots=True)
class RecoveryEpisode:
    tool: str
    round: int
    recovered: bool = False


class RecoveryTracker:
    """One episode per failed invocation; success consumes the most recent error.

    A repair must occur in one of the next TWO assistant tool rounds. A parallel
    sibling in the same round cannot repair a failure it has not yet observed.
    """

    def __init__(self):
        self.episodes: list[RecoveryEpisode] = []

    def failed(self, tool: str, round_index: int):
        if round_index < 1:
            raise ValueError("round starts at one")
        self.episodes.append(RecoveryEpisode(tool, round_index))

    def succeeded(self, tool: str, round_index: int):
        for episode in reversed(self.episodes):
            distance = round_index - episode.round
            if episode.tool == tool and not episode.recovered and 1 <= distance <= 2:
                episode.recovered = True
                break

    @property
    def recovered(self) -> int:
        return sum(e.recovered for e in self.episodes)


def recompute_report(report: dict) -> list[dict]:
    """Recompute from raw case records without calling a model or database."""
    if report.get("mode") != "model" or report.get("status") != "completed":
        raise ValueError("a completed model report is required")
    groups: dict[tuple[int, EvalGroup], list[CaseEvalRecord]] = {}
    for run in report["runs"]:
        group = run["group"]
        if group not in {"A", "B", "C"}:
            raise ValueError("unknown evaluation group")
        groups.setdefault((run["repeat"], group), []).append(
            CaseEvalRecord.from_dict(run["record"])
        )
    return [
        {"repeat": repeat, **summarize_group(group, rows)}
        for (repeat, group), rows in sorted(groups.items())
    ]


if __name__ == "__main__":
    import argparse
    import json
    from pathlib import Path

    parser = argparse.ArgumentParser(description="Recompute Lesson 27 model metrics offline")
    parser.add_argument("report", type=Path)
    arguments = parser.parse_args()
    print(
        json.dumps(
            recompute_report(json.loads(arguments.report.read_text())), ensure_ascii=False, indent=2
        )
    )
