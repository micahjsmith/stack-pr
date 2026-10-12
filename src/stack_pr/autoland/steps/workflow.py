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
from stack_pr.autoland.steps.execution import StepContext, StepResult
from stack_pr.autoland.steps.view import StepOutcome, StepTone, StepView

if TYPE_CHECKING:
    from stack_pr.autoland.model import StackEntry


# "skipped" is set only by WorkflowStep.assume_completed, i.e. for a workflow
# that sits inside a plan's already-landed prefix — it must have run before
# those PRs could merge, so the plan assumes it did rather than claiming it was
# deliberately skipped.
_STATE_STATUS = {
    "pending": (StepOutcome.PENDING, "Pending", StepTone.MUTED),
    "waiting": (StepOutcome.ACTIVE, "Waiting for workflow", StepTone.AWAITING_WORKFLOW),
    "succeeded": (StepOutcome.DONE, "Workflow complete", StepTone.SUCCESS),
    "failed": (StepOutcome.FAILED, "Failed", StepTone.FAILURE),
    "skipped": (StepOutcome.DONE, "Assumed completed", StepTone.SUCCESS),
}


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

    def view(
        self, stack: list[StackEntry], *, is_next: bool, assumed_completed: bool
    ) -> StepView:
        outcome, status, tone = _STATE_STATUS.get(
            self.state, (StepOutcome.PENDING, "Pending", StepTone.MUTED)
        )
        return StepView(
            outcome=outcome,
            status=status,
            tone=tone,
            key=self.workflow,
            description=f"Wait for workflow {self.workflow}",
            detail=(self.error_message,) if self.error_message else (),
            data={"workflow": self.workflow},
        )

    def execute(self, sc: StepContext) -> StepResult:
        """Wait for a successful run of the workflow that includes the code
        landed so far."""
        # "succeeded" before the run reaches it means --replan carried the
        # result over from the run this one replaced.
        if self.state in ("skipped", "succeeded"):
            return StepResult.done()
        sc.services.ui.print(sc.banner(f"Workflow checkpoint — {self.workflow}"))
        if not sc.services.waits.wait_for_workflow(
            self, sc.landed_sha(), opts=sc.opts, common=sc.common, ctx=sc.run
        ):
            return StepResult.abort(f"Workflow {self.workflow} failed or timed out")
        return StepResult.done()
