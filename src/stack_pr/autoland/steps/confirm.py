"""``c [<condition>]``: pause for manual confirmation."""

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


@register_step
@dataclass
class ConfirmStep(AutolandStep):
    """Pause for manual confirmation before continuing.

    ``condition`` is an optional human-readable thing to verify before
    proceeding (e.g. ``"QA sign-off complete"``). When set, it is shown in the
    confirmation prompt; when empty, a generic prompt is shown. Either way the
    step waits until the user types ``y``/``Y`` and presses Enter.
    """

    kind = "confirm"
    keyword = "c"
    label = "Confirm"
    # A condition is free text that routinely runs long; letting it decide the
    # comment column would drag every comment off to the right.
    aligns_comments = False

    condition: str = ""
    confirmed: bool = False

    @classmethod
    def parse(cls, args: str, parser: PlanParser) -> ConfirmStep:
        # The condition is optional: a bare 'c' just pauses to confirm.
        return cls(condition=args)

    def plan_line(self, stack: list[StackEntry]) -> tuple[str, str]:
        return f"c {self.condition}".rstrip(), ""

    def assume_completed(self) -> None:
        self.confirmed = True

    def is_checkpoint_done(self) -> bool:
        return self.confirmed

    def replan_key(self, landed_prs: frozenset[int]) -> ReplanKey | None:
        return (self.keyword, self.condition.strip(), landed_prs)

    def credit_from(self, old: AutolandStep) -> None:
        self.confirmed = True

    def view(
        self, stack: list[StackEntry], *, is_next: bool, assumed_completed: bool
    ) -> StepView:
        if self.confirmed:
            # A confirmation inside the landed prefix was never actually given
            # in this run — it is inferred from the fact that later PRs merged
            # — so say so rather than claiming the user confirmed it.
            status = "Assumed completed" if assumed_completed else "Confirmed"
            outcome, tone = StepOutcome.DONE, StepTone.SUCCESS
        elif is_next:
            outcome, status, tone = (
                StepOutcome.ACTIVE,
                "Awaiting confirmation",
                StepTone.AWAITING_USER,
            )
        else:
            outcome, status, tone = StepOutcome.PENDING, "Pending", StepTone.MUTED
        return StepView(
            outcome=outcome,
            status=status,
            tone=tone,
            key=self.condition or "(no condition)",
            description=(
                f"Manual confirmation: {self.condition}"
                if self.condition
                else "Manual confirmation"
            ),
            data={"condition": self.condition},
        )

    def execute(self, sc: StepContext) -> StepResult:
        """Show what comes next, and wait for the user to type y or Y."""
        if self.confirmed:
            return StepResult.done()
        ui = sc.services.ui
        question = (
            f'Confirm "{ui.escape(self.condition)}" is complete — ready to proceed?'
            if self.condition
            else "Ready to proceed?"
        )
        lines = [
            sc.banner("Manual confirmation required", style="bold yellow") + "\n",
            f"[bold]{question}[/bold]\n",
        ]
        upcoming = sc.upcoming()
        if upcoming:
            lines.append("[bold]Next steps:[/bold]")
            lines.extend(f"  {n}. {ui.escape(d)}" for n, d in enumerate(upcoming, 1))
        else:
            lines.append("[dim]This is the final step in the plan.[/dim]")
        ui.print("\n".join(lines))
        while True:
            try:
                answer = ui.input(
                    "[yellow]Type y/Y then Enter to continue (Ctrl+C to abort): "
                    "[/yellow]"
                ).strip()
            except EOFError:
                return StepResult.abort(
                    "Confirm step received EOF — cannot confirm in non-interactive mode"
                )
            if answer in ("y", "Y"):
                break
            ui.print("[dim]Type 'y' or 'Y' to confirm.[/dim]")
        self.confirmed = True
        ui.print("[green]Confirmed[/green]")
        return StepResult.done()
