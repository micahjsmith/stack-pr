"""What a step is given when it executes (``StepContext``), and what it reports
back (``StepResult``).

The engine owns the run: it walks the plan, checkpoints, records run-level
facts, and stops on an abort. A step owns only its own work. It reads the run
through its ``StepContext``, uses the tools in ``StepContext.services``, may
update its own fields (and the stack entry it lands), and reports the outcome
and any run-level facts in its ``StepResult``, which the engine records.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from stack_pr import cli
    from stack_pr.autoland.model import LandingContext
    from stack_pr.autoland.options import AutolandOptions
    from stack_pr.autoland.services import Services


@dataclass(frozen=True)
class StepResult:
    """How a step's execution ended.

    ``abort_reason`` is empty when the step succeeded (or had nothing to do),
    and otherwise says why the run has to stop. ``landed_sha`` is the commit of
    the code the step landed, or "" if it landed nothing or the commit could
    not be found; the engine records it as the run's last landed SHA.
    """

    abort_reason: str = ""
    landed_sha: str = ""

    @property
    def aborted(self) -> bool:
        return bool(self.abort_reason)

    @classmethod
    def done(cls, *, landed_sha: str = "") -> StepResult:
        return cls(landed_sha=landed_sha)

    @classmethod
    def abort(cls, reason: str, *, landed_sha: str = "") -> StepResult:
        if not reason:
            raise ValueError("an aborted step needs a reason")
        return cls(abort_reason=reason, landed_sha=landed_sha)


@dataclass(frozen=True)
class StepContext:
    """What a step executing at plan position ``index`` may see and use."""

    # The step's 0-based position in the plan, and the plan's length.
    index: int
    total: int
    # The run's state. A step may update its own fields and the stack entry it
    # lands, but reports run-level facts through its StepResult instead.
    run: LandingContext
    opts: AutolandOptions
    common: cli.CommonArgs
    services: Services
    # Save the run's state now, so a resume picks up from here.
    checkpoint: Callable[[], None]
    # One-line descriptions of the steps after this one, in plan order.
    upcoming: Callable[[], list[str]]
    # The commit of the code landed so far: the last SHA a step reported, or,
    # if none has been, the head of the target branch (which the engine then
    # records as the run's landed SHA). "" if it can't be found.
    landed_sha: Callable[[], str]
    # Whether this step is the last of the plan's already-landed prefix (see
    # ``plan.landed_prefix_end``), the one whose landed code later steps follow.
    ends_landed_prefix: bool = False

    def banner(self, title: str, style: str = "bold") -> str:
        """The markup heading this step's output: "Step i/n: <title>"."""
        rule = "=" * 60
        return (
            f"\n{rule}\n[{style}]Step {self.index + 1}/{self.total}: "
            f"{title}[/{style}]\n{rule}"
        )
