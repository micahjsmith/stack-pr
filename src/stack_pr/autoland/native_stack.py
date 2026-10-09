"""Merging a run of consecutive land steps as one GitHub stack."""

from __future__ import annotations

import contextlib
from dataclasses import dataclass

from stack_pr import cli
from stack_pr.autoland import gh, runtime, waits
from stack_pr.autoland.model import LandingContext, LandStep, PRState, StackEntry
from stack_pr.autoland.options import AutolandOptions
from stack_pr.autoland.waits import _poll_read

# ---------------------------------------------------------------------------
# Merging a run of land steps as one GitHub stack
# ---------------------------------------------------------------------------


def native_stack_run(ctx: LandingContext, start: int) -> list[StackEntry]:
    """The PRs a stack merge starting at plan step *start* would land.

    That is the run of consecutive ``l`` steps from *start* whose PRs are still
    open. A ``w`` or ``c`` step ends the run: it has to see the PRs above it
    land separately from the ones below it. A run of fewer than two PRs gains
    nothing from a stack merge, so it comes back empty.
    """
    entries: list[StackEntry] = []
    for step in ctx.plan[start:]:
        if not isinstance(step, LandStep) or step.already_landed:
            break
        entry = ctx.stack[step.entry_index]
        if entry.state == PRState.MERGED:
            break
        entries.append(entry)
    return entries if len(entries) > 1 else []


def native_stack_runs(ctx: LandingContext) -> list[tuple[int, list[StackEntry]]]:
    """Every ``(first step, PRs)`` run the rest of the plan would stack-merge."""
    runs = []
    index = ctx.current_step
    while index < len(ctx.plan):
        entries = native_stack_run(ctx, index)
        if entries:
            runs.append((index, entries))
        index += max(len(entries), 1)
    return runs


def print_native_stack_runs(ctx: LandingContext, opts: AutolandOptions) -> None:
    if not opts.merge_as_stack:
        return
    for first, entries in native_stack_runs(ctx):
        prs = ", ".join(f"#{e.pr_number}" for e in entries)
        runtime.console.print(
            f"[dim]Steps {first + 1}-{first + len(entries)} ({prs}) will merge "
            "together as a GitHub stack.[/dim]"
        )


def _open_native_stack_prs(native_stack: dict) -> list[int]:
    """The numbers of a GitHub stack's unmerged PRs, bottom first."""
    return [
        pr["number"]
        for pr in native_stack.get("pull_requests", [])
        if not pr.get("merged_at") and pr.get("state", "open") == "open"
    ]


def _native_stack_for(prs: list[int]) -> tuple[int | None, bool]:
    """Find or create a GitHub stack whose bottom open PRs are exactly *prs*.

    Returns ``(stack number, whether it holds just the run)``, with a ``None``
    number when the run can't be merged as a stack. A stack holding just the
    run is ours to dissolve if the merge falls through, even when it predates
    this call: it is most likely one an interrupted run left behind.
    """
    try:
        existing = gh.github.find_native_stack(prs[0])
    except RuntimeError as e:
        runtime.console.print(f"[yellow]Could not look up GitHub stacks: {e}[/yellow]")
        return None, False

    if existing is None:
        try:
            return gh.github.create_native_stack(prs)["number"], True
        except (RuntimeError, KeyError) as e:
            # The POST may have created the stack even though it looked failed.
            with contextlib.suppress(RuntimeError):
                existing = gh.github.find_native_stack(prs[0])
            if existing is not None and _open_native_stack_prs(existing) == prs:
                return existing["number"], True
            runtime.console.print(
                f"[yellow]Could not create a GitHub stack: {e}[/yellow]"
            )
            return None, False

    # A stack made earlier — by an interrupted run, or by hand with gh stack —
    # is reusable if the run sits at its bottom: merging the run's top PR then
    # merges exactly the run. Anything else would merge PRs the plan doesn't.
    open_prs = _open_native_stack_prs(existing)
    if open_prs[: len(prs)] == prs:
        return existing["number"], open_prs == prs
    runtime.console.print(
        f"[yellow]PR #{prs[0]} is already in GitHub stack #{existing['number']}, "
        "which does not match the plan.[/yellow]"
    )
    return None, False


@dataclass
class NativeStackMergeResult:
    landed: bool = False  # every PR in the run merged
    # Stop the plan. Otherwise, the PRs of the run that are still open should be
    # landed one at a time instead.
    abort_reason: str = ""


def _await_native_stack_merge(
    entries: list[StackEntry],
    uuid: str | None,
    *,
    opts: AutolandOptions,
    ctx: LandingContext,
) -> str:
    """Wait for a requested stack merge to finish.

    Returns "" once every PR merged, else why it didn't. A failure may still
    have merged some of the PRs: GitHub stops a stack merge at the PR that
    failed, and keeps the ones below it.
    """
    top = entries[-1]
    awake_elapsed = 0.0
    while True:
        if ctx.aborted:
            return "aborted"
        if awake_elapsed > opts.merge_timeout:
            return "timed out waiting for the stack to merge"

        still_open = []
        # Whether some PR's state couldn't be read this poll, in which case one
        # counted as still open may already have merged and left the queue.
        unread = False
        for entry in entries:
            if entry.state == PRState.MERGED:
                continue
            state = _poll_read(gh.github.pr_state, entry.pr_number)
            unread = unread or state is None
            if state == "MERGED":
                entry.state = PRState.MERGED
                entry.error_message = ""
            elif state == "CLOSED":
                return f"PR #{entry.pr_number} was closed"
            else:
                still_open.append(entry)
        if not still_open:
            return ""

        status, message = "", ""
        if uuid:
            with contextlib.suppress(RuntimeError):
                status, message = gh.github.merge_async_status(top.pr_number, uuid)
        if status == "failed":
            return message or "GitHub reported the stack merge as failed"
        # Every PR enters the queue together, and GitHub drops the PRs above any
        # PR that leaves it, so the lowest open PR leaving means the merge is
        # off. A request still "pending" hasn't reached the queue yet; without
        # a request id to ask, give it one interval to get there.
        settled = not unread and (
            status == "enqueued" or (not uuid and awake_elapsed > 0)
        )
        # A failed lookup (None) leaves the merge undecided; keep polling.
        if settled and gh.github.in_merge_queue(still_open[0].pr_number) is False:
            return f"PR #{still_open[0].pr_number} was booted from the merge queue"

        mins = int(awake_elapsed) // 60
        for entry in still_open:
            entry.error_message = f"Merging as a stack ({mins}m elapsed)..."
        runtime.console.print(
            f"[dim]Stack of {len(entries)} PRs up to #{top.pr_number}: "
            f"{status or 'merging'} ({mins}m) — polling in {opts.poll_interval}s"
            "[/dim]"
        )
        runtime.resilient_sleep(opts.poll_interval)
        awake_elapsed += opts.poll_interval


def land_as_native_stack(
    entries: list[StackEntry],
    *,
    ctx: LandingContext,
    common: cli.CommonArgs,
    opts: AutolandOptions,
) -> NativeStackMergeResult:
    """Land a run of consecutive PRs (bottom first) with one stack merge.

    Every PR still needs its own approval and passing checks, so those are
    waited for first, exactly as when landing one at a time. Then a single
    merge request on the top PR merges the whole run — through the merge queue
    as one group, where the repo has one — and the stack above is rebased and
    re-submitted once, not once per PR.

    Anything that keeps the run from merging as a stack is not fatal: the
    result asks for the rest to be landed one at a time. A GitHub stack holding
    just the run is dissolved first, since GitHub only lets stacked PRs merge
    through the stack merge API.
    """
    prs = [e.pr_number for e in entries]
    top = entries[-1]
    runtime.console.print(
        f"\n{'=' * 60}\n[bold]Landing {len(entries)} PRs as a GitHub stack: "
        f"{', '.join(f'#{n}' for n in prs)}[/bold]\n{'=' * 60}"
    )

    native_stack_number, dissolvable = _native_stack_for(prs)
    if native_stack_number is None:
        runtime.console.print(
            "[yellow]Landing these PRs one at a time instead.[/yellow]"
        )
        return NativeStackMergeResult()

    failure = ""
    abort_reason = ""
    for entry in entries:
        if not waits.wait_for_approval(entry, opts=opts, ctx=ctx):
            abort_reason = f"PR #{entry.pr_number} approval wait was aborted"
            break
    else:
        for entry in entries:
            if entry.state == PRState.MERGED:
                continue
            if not waits.wait_for_checks(entry, opts=opts, ctx=ctx):
                abort_reason = f"PR #{entry.pr_number} checks failed after retries"
                break

    bottom = next((e for e in entries if e.state != PRState.MERGED), None)
    # Only the lowest open PR is checked here: GitHub reports the ones above it
    # as blocked until it merges. The merge request itself enforces every PR's
    # requirements.
    if (
        not abort_reason
        and bottom is not None
        and not waits.wait_for_mergeable(bottom, opts=opts, ctx=ctx).ready
    ):
        abort_reason = f"PR #{bottom.pr_number} failed to merge"

    if not abort_reason and bottom is not None:
        for entry in entries:
            if entry.state != PRState.MERGED:
                entry.state = PRState.IN_MERGE_QUEUE
                entry.error_message = "Merging as a stack..."
        runtime.console.print(
            f"\n[bold cyan]Merging PRs #{bottom.pr_number}-#{top.pr_number} "
            f"as a stack[/bold cyan]"
        )
        uuid: str | None = None
        # The PR above the run is based on the run's top branch. When that
        # branch is deleted after the merge, GitHub retargets the PR to the top
        # PR's base, which is another of the run's branches, deleted along with
        # it, so GitHub closes the PR. Basing it on the target up front keeps it
        # open; the resubmit after the merge restores its real base. A PR above
        # that is in the GitHub stack itself is GitHub's to retarget.
        above = ctx.stack.index(top) + 1
        if dissolvable and above < len(ctx.stack):
            try:
                gh.github.set_base(ctx.stack[above].pr_number, common.target)
            except RuntimeError as e:
                failure = (
                    f"could not base PR #{ctx.stack[above].pr_number} on "
                    f"{common.target}: {e}"
                )
        if not failure:
            try:
                # Ask GitHub rather than trust the config: the request body must
                # match whether the branch really has a queue (see merge_async).
                has_queue = gh.github.has_merge_queue(common.target)
                uuid = gh.github.merge_async(
                    top.pr_number,
                    merge_queue=opts.merge_queue if has_queue is None else has_queue,
                )
            except RuntimeError as e:
                # 409: a merge request for this stack is already in flight (e.g.
                # from a run that was interrupted) — wait for it like our own.
                if "HTTP 409" not in str(e):
                    failure = f"stack merge request failed: {e}"
        if not failure:
            failure = _await_native_stack_merge(entries, uuid, opts=opts, ctx=ctx)
            if failure == "aborted":
                abort_reason = "Stack merge was aborted"

    landed = all(e.state == PRState.MERGED for e in entries)
    if not landed and dissolvable:
        # Leave no stack behind: landing one PR at a time only works on PRs
        # that aren't in one. Queued PRs stay queued.
        try:
            gh.github.unstack_native_stack(native_stack_number)
        except RuntimeError as e:
            runtime.console.print(
                f"[yellow]Could not dissolve GitHub stack #{native_stack_number}: {e}[/yellow]"
            )

    merged = [e for e in entries if e.state == PRState.MERGED]
    if failure and not abort_reason:
        runtime.console.print(
            f"[yellow]Stack merge did not complete ({failure}). Landing the "
            "remaining PRs one at a time.[/yellow]"
        )
        for entry in entries:
            if entry.state != PRState.MERGED:
                entry.state = PRState.PENDING
                entry.error_message = f"Stack merge: {failure}"
    elif landed:
        runtime.console.print(
            f"\n[bold green]PRs {', '.join(f'#{n}' for n in prs)} merged![/bold green]"
        )

    if merged:
        waits._refresh_last_landed_sha(ctx, common, merged[-1].pr_number)
        if ctx.stack.index(merged[-1]) < len(ctx.stack) - 1:
            try:
                waits.rebase_and_resubmit(common)
            except Exception as e:  # noqa: BLE001 - report any resubmit failure
                return NativeStackMergeResult(
                    landed=landed,
                    abort_reason=(
                        f"Rebase failed after merging #{merged[-1].pr_number}: {e}"
                    ),
                )
    return NativeStackMergeResult(landed=landed, abort_reason=abort_reason)
