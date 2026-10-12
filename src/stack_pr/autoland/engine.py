"""Executing a landing plan step by step.

The engine owns the run: it walks the plan from ``ctx.current_step``,
checkpoints before each step, hands each step a ``StepContext``, records the
run-level facts a step reports in its ``StepResult``, and stops on an abort.
What a step does is up to the step (``AutolandStep.execute``).
"""

from __future__ import annotations

import functools

from stack_pr import cli
from stack_pr.autoland.display import _describe_step
from stack_pr.autoland.model import LandingContext
from stack_pr.autoland.native_stack import land_as_native_stack, native_stack_run
from stack_pr.autoland.options import AutolandOptions
from stack_pr.autoland.plan import landed_prefix_end
from stack_pr.autoland.services import Services
from stack_pr.autoland.state import AutolandCheckpointer
from stack_pr.autoland.steps import StepContext


def execute_plan(
    ctx: LandingContext,
    common: cli.CommonArgs,
    opts: AutolandOptions,
    checkpointer: AutolandCheckpointer,
    *,
    services: Services | None = None,
) -> bool:
    """Execute the landing plan from ctx.current_step. Returns True on success."""
    if services is None:
        services = Services.production()
    prefix_end = landed_prefix_end(ctx.plan)
    native_stack = _NativeStackMerge(ctx, common, opts, checkpointer)

    def upcoming(index: int) -> list[str]:
        return [_describe_step(s, ctx) for s in ctx.plan[index + 1 :]]

    def landed_sha() -> str:
        if not ctx.last_landed_sha:
            _record_landed_sha(ctx, services.repo.landed_sha(common))
        return ctx.last_landed_sha

    for step_idx in range(ctx.current_step, len(ctx.plan)):
        step = ctx.plan[step_idx]
        ctx.current_step = step_idx
        checkpointer.save(ctx)

        abort_reason = native_stack.before_step(step_idx)
        if abort_reason:
            return _abort(ctx, checkpointer, abort_reason)

        result = step.execute(
            StepContext(
                index=step_idx,
                total=len(ctx.plan),
                run=ctx,
                opts=opts,
                common=common,
                services=services,
                checkpoint=lambda: checkpointer.save(ctx),
                upcoming=functools.partial(upcoming, step_idx),
                landed_sha=landed_sha,
                ends_landed_prefix=step_idx == prefix_end,
            )
        )
        _record_landed_sha(ctx, result.landed_sha)
        if result.aborted:
            return _abort(ctx, checkpointer, result.abort_reason)

    ctx.current_step = len(ctx.plan)
    checkpointer.save(ctx)
    return True


def _record_landed_sha(ctx: LandingContext, sha: str) -> None:
    # An unknown SHA ("") keeps the last one known.
    if sha:
        ctx.last_landed_sha = sha


def _abort(
    ctx: LandingContext, checkpointer: AutolandCheckpointer, reason: str
) -> bool:
    ctx.abort_reason = reason
    ctx.aborted = True
    checkpointer.save(ctx)
    return False


class _NativeStackMerge:
    """TEMPORARY: merge a run of consecutive land steps as one GitHub stack.

    With ``--merge-as-stack``, before each step the engine asks whether a run
    of two or more open land steps starts there, and if so lands the run with
    one stack merge (``native_stack.land_as_native_stack``). The land steps
    then find their PRs merged and only skip. A run whose stack merge fell
    through is landed one step at a time instead, without retrying the merge.

    This lives in the engine, not in ``LandStep``, because steps are
    independent of each other. It is a stopgap: a plan-optimization pass that
    groups land steps into one execution unit replaces it.
    """

    def __init__(
        self,
        ctx: LandingContext,
        common: cli.CommonArgs,
        opts: AutolandOptions,
        checkpointer: AutolandCheckpointer,
    ) -> None:
        self._ctx = ctx
        self._common = common
        self._opts = opts
        self._checkpointer = checkpointer
        # The last step of a run whose stack merge fell through.
        self._unstacked_through = -1

    def before_step(self, step_idx: int) -> str:
        """Stack-merge the run starting at *step_idx*, if any. Returns the
        reason the plan must stop, or ""."""
        if not self._opts.merge_as_stack or step_idx <= self._unstacked_through:
            return ""
        run_entries = native_stack_run(self._ctx, step_idx)
        if not run_entries:
            return ""
        result = land_as_native_stack(
            run_entries, ctx=self._ctx, common=self._common, opts=self._opts
        )
        if result.abort_reason:
            return result.abort_reason
        if not result.landed:
            self._unstacked_through = step_idx + len(run_entries) - 1
        self._checkpointer.save(self._ctx)
        return ""
