"""``x <command>``: run a command, and stop the land unless it succeeds."""

from __future__ import annotations

import shlex
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


# As for WorkflowStep, "skipped" is set only by assume_completed: the command
# sits inside the plan's already-landed prefix, so it must have run before the
# PRs after it could merge.
_STATE_STATUS = {
    "pending": (StepOutcome.PENDING, "Pending", StepTone.MUTED),
    "running": (StepOutcome.ACTIVE, "Running", StepTone.RUNNING),
    "succeeded": (StepOutcome.DONE, "Succeeded", StepTone.SUCCESS),
    "failed": (StepOutcome.FAILED, "Failed", StepTone.FAILURE),
    "skipped": (StepOutcome.DONE, "Assumed completed", StepTone.SUCCESS),
}


def _split(command: str) -> list[str]:
    """*command* as an argument list, split the way a POSIX shell would split
    it (quotes, backslashes), but with no expansion: there is no shell."""
    return shlex.split(command)


@register_step
@dataclass
class ExecStep(AutolandStep):
    """Run a command once the PRs before it have landed; it must exit 0.

    ``command`` is the text after ``x``, as written in the plan. It runs with
    no shell, from the top of the checkout being landed, with the run's
    facts in ``AUTOLAND_*`` environment variables (see ``execute``). Any
    other exit status stops the run. A command that failed, or was
    interrupted, runs again on ``--resume``, so it should be safe to re-run.
    """

    kind = "exec"
    keyword = "x"
    label = "Execute"
    # A bare 'x' names no command; it is not an 'x' step at all.
    bare_keyword_ok = False
    # A command line routinely runs long; letting it decide the comment column
    # would drag every comment off to the right.
    aligns_comments = False

    command: str
    state: str = "pending"  # pending, running, succeeded, failed, skipped
    error_message: str = ""

    @classmethod
    def parse(cls, args: str, parser: PlanParser) -> ExecStep:
        if not args:
            raise ValueError(f"Line {parser.line_num}: 'x' requires a command")
        try:
            _split(args)
        except ValueError as e:
            raise ValueError(
                f"Line {parser.line_num}: malformed command {args!r}: {e}"
            ) from None
        return cls(command=args)

    def plan_line(self, stack: list[StackEntry]) -> tuple[str, str]:
        return f"x {self.command}", ""

    def assume_completed(self) -> None:
        self.state = "skipped"

    def is_checkpoint_done(self) -> bool:
        return self.state in ("succeeded", "skipped")

    def replan_key(self, landed_prs: frozenset[int]) -> ReplanKey | None:
        return (self.keyword, self.command, landed_prs)

    def credit_from(self, old: AutolandStep) -> None:
        if isinstance(old, ExecStep):
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
            key=self.command,
            description=f"Run {self.command}",
            detail=(self.error_message,) if self.error_message else (),
            data={"command": self.command},
        )

    def execute(self, sc: StepContext) -> StepResult:
        """Run the command, with the run's facts in its environment:

        - ``AUTOLAND_LANDED_SHA``: the commit of the code landed so far (see
          ``StepContext.landed_sha``), or "" if it can't be found;
        - ``AUTOLAND_LANDED_PRS``: the PRs the plan has landed before this
          step, space-separated in plan order, including those that landed in
          an earlier run;
        - ``AUTOLAND_BRANCH``, ``AUTOLAND_REMOTE``, ``AUTOLAND_TARGET``: the
          branch whose stack is landing, and the remote and branch it lands
          into.
        """
        # "succeeded" before the run reaches it means --replan carried the
        # result over from the run this one replaced.
        if self.state in ("skipped", "succeeded"):
            return StepResult.done()
        ui = sc.services.ui
        ui.print(sc.banner(f"Run {ui.escape(self.command)}"))
        stack = sc.run.stack
        landed = [
            pr
            for step in sc.run.plan[: sc.index]
            if (pr := step.landed_pr(stack)) is not None
        ]
        env = {
            "AUTOLAND_LANDED_SHA": sc.landed_sha(),
            "AUTOLAND_LANDED_PRS": " ".join(str(pr) for pr in landed),
            "AUTOLAND_BRANCH": sc.branch,
            "AUTOLAND_REMOTE": sc.common.remote,
            "AUTOLAND_TARGET": sc.common.target,
        }
        self.state, self.error_message = "running", ""
        # So that --status shows the command running, and an interrupted run
        # resumes by running it again.
        sc.checkpoint()
        try:
            status = sc.services.commands.run(
                _split(self.command), cwd=sc.services.repo.root(), env=env
            )
        except OSError as e:
            return self._fail(f"could not start: {e.strerror or e}")
        if status < 0:
            return self._fail(f"killed by signal {-status}")
        if status:
            return self._fail(f"exited with status {status}")
        self.state = "succeeded"
        ui.print("[green]Command succeeded[/green]")
        return StepResult.done()

    def _fail(self, why: str) -> StepResult:
        self.state = "failed"
        self.error_message = f"Command {why}"
        return StepResult.abort(f"Command `{self.command}` {why}")
