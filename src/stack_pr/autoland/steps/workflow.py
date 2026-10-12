"""``w <workflow>``: wait for a GitHub Actions workflow."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from stack_pr.autoland.steps.base import (
    AutolandStep,
    PlanParser,
    ReplanKey,
    register_step,
)

if TYPE_CHECKING:
    from stack_pr.autoland.model import StackEntry


@register_step
@dataclass
class WorkflowStep(AutolandStep):
    """Wait for a GitHub Actions workflow to succeed with the landed code."""

    kind = "workflow"
    keyword = "w"
    label = "Workflow"
    # A bare 'w' names no workflow; it is not a 'w' step at all.
    bare_keyword_ok = False

    workflow: str
    state: str = "pending"  # pending, waiting, succeeded, failed, skipped
    error_message: str = ""

    @classmethod
    def parse(cls, args: str, parser: PlanParser) -> WorkflowStep:
        if not args:
            raise ValueError(f"Line {parser.line_num}: 'w' requires a workflow name")
        return cls(workflow=args)

    def plan_line(self, stack: list[StackEntry]) -> tuple[str, str]:
        return f"w {self.workflow}", ""

    def assume_completed(self) -> None:
        self.state = "skipped"

    def is_checkpoint_done(self) -> bool:
        return self.state in ("succeeded", "skipped")

    def replan_key(self, landed_prs: frozenset[int]) -> ReplanKey | None:
        return (self.keyword, self.workflow, landed_prs)

    def credit_from(self, old: AutolandStep) -> None:
        if isinstance(old, WorkflowStep):
            self.state = old.state
            self.error_message = ""
