"""Bounded model-facing goal and child handoff contracts."""

from decimal import Decimal
from typing import Annotated, Literal

from pydantic import Field, RootModel, model_validator

from compass_agent.schemas import GoalBudget, GoalContract, RunContractModel, RunUUID

OPEN_STATUSES = ("active", "paused", "blocked", "budget_limited")
LEDGER_LIMIT = 400
MAX_TASKS = 4
MAX_PARALLEL = 2
NO_PROGRESS_LIMIT = FAILURE_LIMIT = 3
GOAL_LOOP_INTERVAL = 1


class InputRef(RunContractModel):
    kind: Literal["ledger", "artifact", "evidence"]
    id: str = Field(min_length=1, max_length=200)


class DelegationTask(RunContractModel):
    name: str = Field(min_length=1, max_length=40)
    objective: str = Field(min_length=1, max_length=8000)
    expectations: list[Annotated[str, Field(max_length=500)]] = Field(
        default_factory=list, max_length=8
    )
    constraints: list[Annotated[str, Field(max_length=500)]] = Field(
        default_factory=list, max_length=10
    )
    inputs: list[InputRef] = Field(default_factory=list, max_length=12)
    tools: list[str] = Field(default_factory=list, max_length=8)
    connection_ids: list[RunUUID] = Field(default_factory=list, alias="connectionIds", max_length=4)
    max_steps: int = Field(default=3, alias="maxSteps", ge=3, le=8)


class Finding(RunContractModel):
    statement: str = Field(min_length=1, max_length=500)
    fact_key: str | None = Field(default=None, alias="factKey", pattern=r"^[a-z0-9_.:-]{1,80}$")
    value: Decimal | None = Field(default=None, strict=False)
    unit: str | None = Field(default=None, max_length=20)
    evidence_ids: list[str] = Field(default_factory=list, alias="evidenceIds", max_length=8)


class ChildReturn(RunContractModel):
    status: Literal["completed", "partial", "blocked"]
    summary: str = Field(min_length=1, max_length=1000)
    findings: list[Finding] = Field(default_factory=list, max_length=20)
    artifact_refs: list[str] = Field(default_factory=list, alias="artifactRefs", max_length=8)
    gaps: list[Annotated[str, Field(max_length=500)]] = Field(default_factory=list, max_length=10)
    blockers: list[Annotated[str, Field(max_length=500)]] = Field(
        default_factory=list, max_length=5
    )
    next_actions: list[Annotated[str, Field(max_length=500)]] = Field(
        default_factory=list, alias="nextActions", max_length=5
    )


class ChildDelivery(RunContractModel):
    delivery_id: RunUUID = Field(alias="deliveryId")
    child_run_id: RunUUID = Field(alias="childRunId")
    reason: Literal["returned", "natural_end", "step_limit", "budget", "error"]
    status: Literal["completed", "partial", "blocked", "failed"]
    result: ChildReturn | None
    rejected_evidence_ids: list[str] = Field(default_factory=list, alias="rejectedEvidenceIds")
    answer: str
    evidence_ids: list[str] = Field(default_factory=list, alias="evidenceIds")


class ProgressReport(RunContractModel):
    kind: Literal["progress"]
    summary: str = Field(min_length=1, max_length=120)


class FinalReport(RunContractModel):
    kind: Literal["final"]
    result: ChildReturn


class ReportToParentInput(
    RootModel[Annotated[ProgressReport | FinalReport, Field(discriminator="kind")]]
):
    pass


class ProposeGoal(RunContractModel):
    op: Literal["propose"]
    contract: GoalContract
    budget: GoalBudget = Field(default_factory=GoalBudget)
    goal_id: RunUUID | None = Field(default=None, alias="goalId")
    expected_revision: int | None = Field(default=None, alias="expectedRevision", ge=1)


class LedgerItem(RunContractModel):
    type: Literal["conclusion", "todo", "todo_done", "failure"]
    key: str = Field(pattern=r"^[a-z0-9_.:-]{1,80}$")
    title: str = Field(min_length=1, max_length=200)
    text: str | None = Field(default=None, max_length=2000)
    evidence_ids: list[str] = Field(default_factory=list, alias="evidenceIds", max_length=8)
    fact_key: str | None = Field(default=None, alias="factKey", pattern=r"^[a-z0-9_.:-]{1,80}$")
    value: Decimal | None = Field(default=None, strict=False)
    unit: str | None = Field(default=None, max_length=20)
    acceptance_ids: list[str] = Field(default_factory=list, alias="acceptanceIds", max_length=10)
    depends_on: list[str] = Field(default_factory=list, alias="dependsOn", max_length=16)
    resolves: list[str] = Field(default_factory=list, max_length=10)
    required: bool = True

    @model_validator(mode="after")
    def conclusion_needs_evidence(self):
        if self.type == "conclusion" and not self.evidence_ids:
            raise ValueError("conclusion requires evidenceIds")
        return self


class RecordProgress(RunContractModel):
    op: Literal["record"]
    items: list[LedgerItem] = Field(min_length=1, max_length=10)


class CompleteGoal(RunContractModel):
    op: Literal["complete"]
    expected_revision: int = Field(alias="expectedRevision", ge=1)


class BlockGoal(RunContractModel):
    op: Literal["block"]
    reason: str = Field(min_length=1, max_length=60)


class GoalInput(
    RootModel[
        Annotated[
            ProposeGoal | RecordProgress | CompleteGoal | BlockGoal, Field(discriminator="op")
        ]
    ]
):
    pass
