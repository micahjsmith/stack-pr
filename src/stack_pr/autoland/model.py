"""Autoland's data types: the stack, the plan's steps, and the landing context."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Union

# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


class PRState(str, Enum):
    PENDING = "pending"
    WAITING_FOR_APPROVAL = "waiting_for_approval"
    WAITING_FOR_CHECKS = "waiting_for_checks"
    IN_MERGE_QUEUE = "in_merge_queue"
    WAITING_FOR_WORKFLOW = "waiting_for_workflow"
    MERGED = "merged"
    FAILED = "failed"


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


@dataclass
class LandStep:
    """Land a PR in the stack through the merge queue.

    ``entry_index`` indexes into ``LandingContext.stack``, or is ``-1`` for a
    PR that has already landed: such a PR is no longer part of the stack, so
    the step is skipped at execution time.

    ``pr_number`` is set when the plan pinned a specific PR (``l 123``) and is
    ``None`` for a bare, positional ``l``. It is the only thing identifying an
    already-landed step, so it is always set when ``entry_index`` is ``-1``.
    """

    entry_index: int
    pr_number: int | None = None

    @property
    def already_landed(self) -> bool:
        return self.entry_index < 0


@dataclass
class WorkflowStep:
    """Wait for a GitHub Actions workflow to succeed with the landed code."""

    workflow: str
    state: str = "pending"  # pending, waiting, succeeded, failed, skipped
    error_message: str = ""


@dataclass
class ConfirmStep:
    """Pause for manual confirmation before continuing.

    ``condition`` is an optional human-readable thing to verify before
    proceeding (e.g. ``"QA sign-off complete"``). When set, it is shown in the
    confirmation prompt; when empty, a generic prompt is shown. Either way the
    step waits until the user types ``y``/``Y`` and presses Enter.
    """

    condition: str = ""
    confirmed: bool = False


PlanStep = Union[LandStep, WorkflowStep, ConfirmStep]


@dataclass
class LandingContext:
    """Mutable state for the landing run."""

    stack: list[StackEntry] = field(default_factory=list)
    plan: list[PlanStep] = field(default_factory=list)
    current_step: int = 0
    current_index: int = 0  # index into stack for the active land step
    aborted: bool = False
    abort_reason: str = ""
    last_landed_sha: str = ""  # merge commit of the last landed PR

    def abort_requested(self) -> bool:
        # A method, not a plain attribute read, so that mypy does not carry
        # narrowing of `aborted` across calls (or the SIGINT handler) that set it.
        return self.aborted


_STEP_TYPES = {LandStep: "land", WorkflowStep: "workflow", ConfirmStep: "confirm"}


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
    # dataclasses.asdict gives the fields; tag the type for deserialization.
    return {"type": _STEP_TYPES[type(step)], **asdict(step)}


def _deserialize_step(data: dict) -> PlanStep:
    fields = {k: v for k, v in data.items() if k != "type"}
    if data["type"] == "land":
        return LandStep(**fields)
    if data["type"] == "workflow":
        return WorkflowStep(**fields)
    return ConfirmStep(**fields)
