"""Executing a landing plan.

The engine owns the run. It optimizes the rest of the plan into execution
units (``optimize.optimize_plan``), then executes them in order. Through the
unit's ``UnitContext``, each step the unit runs is made the current step and
checkpointed before it starts, is handed a ``StepContext``, and has the
run-level facts it reports in its ``StepResult`` recorded. The run stops on
the first abort. What a step does is up to the step (``AutolandStep.execute``),
and what a unit does across its steps is up to the policy that built it.
"""

from __future__ import annotations

import functools

from stack_pr import cli
from stack_pr.autoland.display import _describe_step
from stack_pr.autoland.model import LandingContext
from stack_pr.autoland.optimize import PolicyRegistry, UnitContext, optimize_plan
from stack_pr.autoland.options import AutolandOptions
from stack_pr.autoland.plan import landed_prefix_end
from stack_pr.autoland.services import UI, Services
from stack_pr.autoland.state import AutolandCheckpointer
from stack_pr.autoland.steps import StepContext, StepResult


def execute_plan(
    ctx: LandingContext,
    common: cli.CommonArgs,
    opts: AutolandOptions,
    checkpointer: AutolandCheckpointer,
    *,
    services: Services | None = None,
    policies: PolicyRegistry | None = None,
) -> bool:
    """Execute the landing plan from ctx.current_step. Returns True on success.

    *policies* defaults to every registered optimization policy.
    """
    if services is None:
        services = Services.production()
    prefix_end = landed_prefix_end(ctx.plan)

    def upcoming(index: int) -> list[str]:
        return [_describe_step(s, ctx) for s in ctx.plan[index + 1 :]]

    def landed_sha() -> str:
        if not ctx.last_landed_sha:
            _record_landed_sha(ctx, services.repo.landed_sha(common))
        return ctx.last_landed_sha

    def checkpoint() -> None:
        checkpointer.save(ctx)

    def enter(index: int) -> None:
        ctx.current_step = index
        checkpoint()

    def execute_step(index: int) -> StepResult:
        result = ctx.plan[index].execute(
            StepContext(
                index=index,
                total=len(ctx.plan),
                run=ctx,
                opts=opts,
                common=common,
                services=services,
                checkpoint=checkpoint,
                upcoming=functools.partial(upcoming, index),
                landed_sha=landed_sha,
                ends_landed_prefix=index == prefix_end,
                branch=checkpointer.branch,
            )
        )
        _record_landed_sha(ctx, result.landed_sha)
        return result

    uc = UnitContext(
        run=ctx,
        opts=opts,
        common=common,
        services=services,
        enter=enter,
        execute_step=execute_step,
        checkpoint=checkpoint,
    )
    for unit in optimize_plan(ctx, opts, policies):
        abort_reason = unit.execute(uc)
        if abort_reason:
            return _abort(ctx, checkpointer, abort_reason)

    ctx.current_step = len(ctx.plan)
    checkpointer.save(ctx)
    return True


def print_execution_preview(
    ctx: LandingContext,
    opts: AutolandOptions,
    *,
    ui: UI,
    policies: PolicyRegistry | None = None,
) -> None:
    """Announce what the optimized plan does beyond executing its steps one by
    one. Derived from the same units ``execute_plan`` executes."""
    for unit in optimize_plan(ctx, opts, policies):
        for line in unit.preview():
            ui.print(line)


def _record_landed_sha(ctx: LandingContext, sha: str) -> None:
    # An unknown SHA ("") keeps the last one known.
    if sha:
        ctx.last_landed_sha = sha


def _abort(
    ctx: LandingContext, checkpointer: AutolandCheckpointer, reason: str
) -> bool:
    ctx.abort_reason = reason
    checkpointer.save(ctx)
    return False
