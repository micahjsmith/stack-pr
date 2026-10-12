"""``--merge-as-stack``: land runs of consecutive land steps with one stack merge.

GitHub's mechanics of a stack merge live in ``autoland.native_stack``; this
module decides which steps it covers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from stack_pr.autoland.native_stack import land_as_native_stack
from stack_pr.autoland.optimize.base import (
    ExecutionUnit,
    StepUnit,
    UnitContext,
    register_policy,
)
from stack_pr.autoland.pr_state import PRState
from stack_pr.autoland.steps import LandStep

if TYPE_CHECKING:
    from collections.abc import Iterable

    from stack_pr.autoland.model import LandingContext, StackEntry
    from stack_pr.autoland.options import AutolandOptions


def _open_land_entry(run: LandingContext, index: int) -> StackEntry | None:
    """The still-open PR plan step *index* lands, if it is a land step."""
    step = run.plan[index]
    if not isinstance(step, LandStep) or step.already_landed:
        return None
    entry = run.stack[step.entry_index]
    return None if entry.state == PRState.MERGED else entry


def stack_merge_run(run: LandingContext, indices: Iterable[int]) -> list[StackEntry]:
    """The PRs a stack merge starting at the first of *indices* would land.

    That is the run of consecutive land steps, from the first of *indices*,
    whose PRs are still open. Any other step ends the run: a ``w``, ``c``, or ``x``
    step has to see the PRs above it land separately from the ones below it.
    A run of fewer than two PRs gains nothing from a stack merge, so it comes
    back empty.
    """
    entries: list[StackEntry] = []
    for index in indices:
        entry = _open_land_entry(run, index)
        if entry is None:
            break
        entries.append(entry)
    return entries if len(entries) > 1 else []


@dataclass(frozen=True)
class StackMergeUnit(ExecutionUnit):
    """A run of consecutive open land steps, landed with one stack merge.

    ``entries`` are the run's PRs when the plan was optimized, for the
    preview. The unit re-validates the run when it executes: before each of
    its steps (until a merge falls through), it looks again at which of the
    following steps in the unit still land an open PR, and stack-merges those,
    if there are at least two. So a PR merged elsewhere in the meantime
    shortens the run, exactly as if the run were evaluated when its first step
    is reached.

    After a stack merge, each step still executes on its own, so it reports
    its PR as merged, with its landed SHA. If the merge fell through, the steps
    it covered land one at a time without retrying the merge.
    """

    span: range
    entries: tuple[StackEntry, ...]

    @property
    def indices(self) -> range:
        return self.span

    def execute(self, uc: UnitContext) -> str:
        # The last step of a run whose stack merge fell through.
        unstacked_through = -1
        for index in self.span:
            uc.enter(index)
            if index > unstacked_through:
                entries = stack_merge_run(uc.run, range(index, self.span.stop))
                if entries:
                    result = land_as_native_stack(
                        entries,
                        ctx=uc.run,
                        common=uc.common,
                        opts=uc.opts,
                        services=uc.services,
                    )
                    if result.abort_reason:
                        return result.abort_reason
                    if not result.landed:
                        unstacked_through = index + len(entries) - 1
                    uc.checkpoint()
            abort_reason = uc.execute_step(index).abort_reason
            if abort_reason:
                return abort_reason
        return ""

    def preview(self) -> list[str]:
        prs = ", ".join(f"#{e.pr_number}" for e in self.entries)
        return [
            (
                f"[dim]Steps {self.span.start + 1}-{self.span.stop} ({prs}) will "
                "merge together as a GitHub stack.[/dim]"
            )
        ]


@register_policy
class NativeStackMergePolicy:
    """Group each run of two or more consecutive open land steps into a
    ``StackMergeUnit``. On with ``--merge-as-stack``."""

    name = "native-stack-merge"

    def applies(self, opts: AutolandOptions) -> bool:
        return opts.merge_as_stack

    def optimize(
        self,
        units: list[ExecutionUnit],
        run: LandingContext,
        opts: AutolandOptions,  # noqa: ARG002 - part of the policy interface
    ) -> list[ExecutionUnit]:
        optimized: list[ExecutionUnit] = []
        # The StepUnits of the run of open land steps being collected.
        pending: list[StepUnit] = []

        def flush() -> None:
            if not pending:
                return
            first = pending[0].index
            entries = stack_merge_run(run, range(first, first + len(pending)))
            if entries:
                optimized.append(
                    StackMergeUnit(
                        span=range(first, first + len(entries)),
                        entries=tuple(entries),
                    )
                )
            else:
                optimized.extend(pending)
            pending.clear()

        for unit in units:
            if (
                isinstance(unit, StepUnit)
                and _open_land_entry(run, unit.index) is not None
            ):
                pending.append(unit)
            else:
                flush()
                optimized.append(unit)
        flush()
        return optimized
