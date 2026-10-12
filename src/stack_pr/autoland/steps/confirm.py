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
