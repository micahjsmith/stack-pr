"""Executing a landing plan step by step."""

from __future__ import annotations

from stack_pr import cli
from stack_pr.autoland import runtime, waits
from stack_pr.autoland.display import _escape_markup, _next_steps_lines
from stack_pr.autoland.model import ConfirmStep, LandingContext, LandStep, WorkflowStep
from stack_pr.autoland.native_stack import land_as_native_stack, native_stack_run
from stack_pr.autoland.options import AutolandOptions
from stack_pr.autoland.plan import landed_prefix_end
from stack_pr.autoland.state import AutolandCheckpointer


def execute_plan(
    ctx: LandingContext,
    common: cli.CommonArgs,
    opts: AutolandOptions,
    checkpointer: AutolandCheckpointer,
) -> bool:
    """Execute the landing plan from ctx.current_step. Returns True on success."""
    # A workflow checkpoint after the already-landed prefix of a plan should
    # wait for the code of the last PR that prefix landed. Only that last one
    # matters, so don't pay for a fetch + lookup on each of the others.
    last_prelanded = landed_prefix_end(ctx.plan)
    # The last step of a run whose stack merge fell through; the rest of that
    # run lands one PR at a time rather than retrying the stack merge.
    unstacked_through = -1

    for step_idx in range(ctx.current_step, len(ctx.plan)):
        step = ctx.plan[step_idx]
        ctx.current_step = step_idx
        checkpointer.save(ctx)

        if isinstance(step, LandStep) and step.already_landed:
            runtime.console.print(
                f"\n[green]PR #{step.pr_number} already landed, skipping[/green]"
            )
            if step_idx == last_prelanded:
                waits._refresh_last_landed_sha(ctx, common, step.pr_number)

        elif isinstance(step, LandStep):
            entry = ctx.stack[step.entry_index]
            ctx.current_index = step.entry_index

            run_entries = (
                native_stack_run(ctx, step_idx)
                if opts.merge_as_stack and step_idx > unstacked_through
                else []
            )
            if run_entries:
                result = land_as_native_stack(
                    run_entries, ctx=ctx, common=common, opts=opts
                )
                if result.abort_reason:
                    return _abort(ctx, checkpointer, result.abort_reason)
                if not result.landed:
                    unstacked_through = step_idx + len(run_entries) - 1
                checkpointer.save(ctx)

            if entry.is_merged():
                runtime.console.print(
                    f"\n[green]PR #{entry.pr_number} already merged, skipping[/green]"
                )
                waits._refresh_last_landed_sha(ctx, common, entry.pr_number)
                continue

            runtime.console.print(
                f"\n{'=' * 60}\n[bold]Step {step_idx + 1}/{len(ctx.plan)}: "
                f"Landing PR #{entry.pr_number} — {entry.title}[/bold]\n{'=' * 60}"
            )

            if not waits.wait_for_approval(entry, opts=opts, ctx=ctx):
                return _abort(
                    ctx,
                    checkpointer,
                    f"PR #{entry.pr_number} approval wait was aborted",
                )

            if entry.is_merged():
                waits._refresh_last_landed_sha(ctx, common, entry.pr_number)
            else:
                if not waits.wait_for_checks(entry, opts=opts, ctx=ctx):
                    return _abort(
                        ctx,
                        checkpointer,
                        f"PR #{entry.pr_number} checks failed after retries",
                    )

                if entry.is_merged():
                    waits._refresh_last_landed_sha(ctx, common, entry.pr_number)
                else:
                    if not waits.enqueue_and_wait(entry, opts=opts, ctx=ctx):
                        return _abort(
                            ctx,
                            checkpointer,
                            f"PR #{entry.pr_number} failed to merge",
                        )
                    waits._refresh_last_landed_sha(ctx, common, entry.pr_number)

            # Rebase + resubmit whenever commits remain above the one we just
            # landed — not only when more *land steps* follow. On a partial
            # land, the PRs we're leaving open still need their bases rebased
            # onto the newly-landed commit.
            has_commits_above = step.entry_index < len(ctx.stack) - 1
            if has_commits_above:
                try:
                    waits.rebase_and_resubmit(common)
                except Exception as e:  # noqa: BLE001 - report any resubmit failure
                    return _abort(
                        ctx,
                        checkpointer,
                        f"Rebase failed after merging #{entry.pr_number}: {e}",
                    )

        elif isinstance(step, WorkflowStep):
            # "succeeded" before the loop reaches it means --replan carried the
            # result over from the run this one replaced.
            if step.state in ("skipped", "succeeded"):
                continue
            runtime.console.print(
                f"\n{'=' * 60}\n[bold]Step {step_idx + 1}/{len(ctx.plan)}: "
                f"Workflow checkpoint — {step.workflow}[/bold]\n{'=' * 60}"
            )
            if not ctx.last_landed_sha:
                waits._refresh_last_landed_sha(ctx, common)
            if not waits.wait_for_workflow(step, opts=opts, common=common, ctx=ctx):
                return _abort(
                    ctx,
                    checkpointer,
                    f"Workflow {step.workflow} failed or timed out",
                )

        elif isinstance(step, ConfirmStep):
            if step.confirmed:
                continue
            question = (
                f'Confirm "{_escape_markup(step.condition)}" is complete — '
                "ready to proceed?"
                if step.condition
                else "Ready to proceed?"
            )
            lines = [
                f"\n{'=' * 60}",
                (
                    f"[bold yellow]Step {step_idx + 1}/{len(ctx.plan)}: "
                    "Manual confirmation required[/bold yellow]"
                ),
                f"{'=' * 60}\n",
                f"[bold]{question}[/bold]\n",
            ]
            next_lines = _next_steps_lines(ctx.plan, step_idx, ctx)
            if next_lines:
                lines.append("[bold]Next steps:[/bold]")
                lines.extend(next_lines)
            else:
                lines.append("[dim]This is the final step in the plan.[/dim]")
            runtime.console.print("\n".join(lines))
            while True:
                try:
                    answer = runtime.console.input(
                        "[yellow]Type y/Y then Enter to continue "
                        "(Ctrl+C to abort): [/yellow]"
                    ).strip()
                except EOFError:
                    return _abort(
                        ctx,
                        checkpointer,
                        "Confirm step received EOF — cannot confirm in "
                        "non-interactive mode",
                    )
                if answer in ("y", "Y"):
                    break
                runtime.console.print("[dim]Type 'y' or 'Y' to confirm.[/dim]")
            step.confirmed = True
            runtime.console.print("[green]Confirmed[/green]")

    ctx.current_step = len(ctx.plan)
    checkpointer.save(ctx)
    return True


def _abort(
    ctx: LandingContext, checkpointer: AutolandCheckpointer, reason: str
) -> bool:
    ctx.abort_reason = reason
    ctx.aborted = True
    checkpointer.save(ctx)
    return False
