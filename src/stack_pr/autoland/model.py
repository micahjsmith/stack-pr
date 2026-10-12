"""Autoland's data types: the stack, the plan's steps, and the landing context."""

from __future__ import annotations

from dataclasses import dataclass, field

from stack_pr.autoland.pr_state import PRState
from stack_pr.autoland.steps import (
    AutolandStep,
    ConfirmStep,
    ExecStep,
    LandStep,
    WorkflowStep,
    step_from_dict,
)

__all__ = [
    "AutolandStep",
    "ConfirmStep",
    "ExecStep",
    "LandStep",
    "LandingContext",
    "PRState",
    "PlanStep",
    "StackEntry",
    "WorkflowStep",
]

# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class StackEntry:
    """One PR in the stack (bottom = index 0)."""

    pr_url: str
    pr_number: int
    branch: str
    title: str = ""
    review_decision: str = ""  # APPROVED, REVIEW_REQUIRED, CHANGES_REQUESTED, ""
    state: PRState = PRState.PENDING
    check_retries: int = 0
    queue_retries: int = 0
    error_message: str = ""

    @property
    def is_approved(self) -> bool:
        # GitHub reports no review decision at all when the target branch
        # requires no review, and then there is no approval to wait for.
        return self.review_decision in ("APPROVED", "")

    def is_merged(self) -> bool:
        # A method, not a property, so that mypy does not carry narrowing of
        # `state` across calls (e.g. the wait_* helpers) that update it.
        return self.state == PRState.MERGED


# The step types live in the steps package; re-exported here, where the rest of
# autoland has always imported them from. PlanStep predates the base class.
PlanStep = AutolandStep


@dataclass
class LandingContext:
    """Mutable state for the landing run."""

    stack: list[StackEntry] = field(default_factory=list)
    plan: list[PlanStep] = field(default_factory=list)
    current_step: int = 0
    current_index: int = 0  # index into stack for the active land step
    # Why the run stopped, or "" while it hasn't. Saved with the checkpoint.
    abort_reason: str = ""
    last_landed_sha: str = ""  # merge commit of the last landed PR


def _deserialize_entry(data: dict) -> StackEntry:
    return StackEntry(
        pr_url=data["pr_url"],
        pr_number=data["pr_number"],
        branch=data["branch"],
        title=data.get("title", ""),
        review_decision=data.get("review_decision", ""),
        state=PRState(data.get("state", "pending")),
        check_retries=data.get("check_retries", 0),
        queue_retries=data.get("queue_retries", 0),
        error_message=data.get("error_message", ""),
    )


def _serialize_step(step: PlanStep) -> dict:
    return step.to_dict()


def _deserialize_step(data: dict) -> PlanStep:
    """Raises ValueError for a step type no registered step class handles."""
    return step_from_dict(data)
