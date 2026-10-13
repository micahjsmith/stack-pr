"""Merging a run of consecutive land steps as one GitHub stack.

This is GitHub's side of a stack merge. Which steps one covers is decided by
the ``optimize.stack_merge`` policy.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from typing import TYPE_CHECKING

from stack_pr.autoland.model import LandingContext, PRState, StackEntry
from stack_pr.autoland.options import AutolandOptions
from stack_pr.autoland.waits import _poll_read

if TYPE_CHECKING:
    from stack_pr import cli
    from stack_pr.autoland.services import Services

# ---------------------------------------------------------------------------
# Merging a run of land steps as one GitHub stack
# ---------------------------------------------------------------------------


def _open_native_stack_prs(native_stack: dict) -> list[int]:
    """The numbers of a GitHub stack's unmerged PRs, bottom first."""
    return [
        pr["number"]
        for pr in native_stack.get("pull_requests", [])
        if not pr.get("merged_at") and pr.get("state", "open") == "open"
    ]


def _native_stack_for(prs: list[int], services: Services) -> tuple[int | None, bool]:
    """Find or create a GitHub stack whose bottom open PRs are exactly *prs*.

    Returns ``(stack number, whether it holds just the run)``, with a ``None``
    number when the run can't be merged as a stack. A stack holding just the
    run is ours to dissolve if the merge falls through, even when it predates
    this call: it is most likely one an interrupted run left behind.
    """
    ui, github = services.ui, services.github
    try:
        existing = github.find_native_stack(prs[0])
    except RuntimeError as e:
        ui.print(f"[yellow]Could not look up GitHub stacks: {e}[/yellow]")
        return None, False

    if existing is None:
        try:
            return github.create_native_stack(prs)["number"], True
        except (RuntimeError, KeyError) as e:
            # The POST may have created the stack even though it looked failed.
            with contextlib.suppress(RuntimeError):
                existing = github.find_native_stack(prs[0])
            if existing is not None and _open_native_stack_prs(existing) == prs:
                return existing["number"], True
            ui.print(f"[yellow]Could not create a GitHub stack: {e}[/yellow]")
            return None, False

    # A stack made earlier — by an interrupted run, or by hand with gh stack —
    # is reusable if the run sits at its bottom: merging the run's top PR then
    # merges exactly the run. Anything else would merge PRs the plan doesn't.
    open_prs = _open_native_stack_prs(existing)
    if open_prs[: len(prs)] == prs:
        return existing["number"], open_prs == prs
    ui.print(
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
    services: Services,
) -> str:
    """Wait for a requested stack merge to finish.

    Returns "" once every PR merged, else why it didn't. A failure may still
    have merged some of the PRs: GitHub stops a stack merge at the PR that
    failed, and keeps the ones below it.
    """
    github = services.github
    top = entries[-1]
    awake_elapsed = 0.0
    while True:
        if awake_elapsed > opts.merge_timeout:
            return "timed out waiting for the stack to merge"

        still_open = []
        # Whether some PR's state couldn't be read this poll, in which case one
        # counted as still open may already have merged and left the queue.
        unread = False
        for entry in entries:
            if entry.state == PRState.MERGED:
                continue
            state = _poll_read(github.pr_state, entry.pr_number, services)
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
                status, message = github.merge_async_status(top.pr_number, uuid)
        if status == "failed":
            return message or "GitHub reported the stack merge as failed"
        # Every PR enters the queue together, and GitHub drops the PRs above any
        # PR that leaves it, so the lowest open PR leaving means the merge is
        # off. A request still "pending" hasn't reached the queue yet; without
        # a request id to ask, give it one interval to get there.
        # Without a queue there is nothing to be booted from: the request
        # merges or fails, and GitHub says which.
        settled = (
            opts.merge_mode.uses_queue
            and not unread
            and (status == "enqueued" or (not uuid and awake_elapsed > 0))
        )
        # A failed lookup (None) leaves the merge undecided; keep polling.
        if settled and github.in_merge_queue(still_open[0].pr_number) is False:
            return f"PR #{still_open[0].pr_number} was booted from the merge queue"

        mins = int(awake_elapsed) // 60
        for entry in still_open:
            entry.error_message = f"Merging as a stack ({mins}m elapsed)..."
        services.ui.print(
            f"[dim]Stack of {len(entries)} PRs up to #{top.pr_number}: "
            f"{status or 'merging'} ({mins}m) — polling in {opts.poll_interval}s"
            "[/dim]"
        )
        services.sleep(opts.poll_interval)
        awake_elapsed += opts.poll_interval


def land_as_native_stack(
    entries: list[StackEntry],
    *,
    ctx: LandingContext,
    common: cli.CommonArgs,
    opts: AutolandOptions,
    services: Services,
) -> NativeStackMergeResult:
    """Land a run of consecutive PRs (bottom first) with one stack merge.

    Every PR still needs its own approval and passing checks, so those are
    waited for first, exactly as when landing one at a time. Then a single
    merge request on the top PR merges the whole run — through the merge queue
    as one group, or directly, per ``opts.merge_mode`` — and the stack above is
    rebased and re-submitted once, not once per PR.

    Anything that keeps the run from merging as a stack is not fatal: the
    result asks for the rest to be landed one at a time. A GitHub stack holding
    just the run is dissolved first, since GitHub only lets stacked PRs merge
    through the stack merge API.
    """
    ui, github, waits, repo = (
        services.ui,
        services.github,
        services.waits,
        services.repo,
    )
    prs = [e.pr_number for e in entries]
    top = entries[-1]
    ui.print(
        f"\n{'=' * 60}\n[bold]Landing {len(entries)} PRs as a GitHub stack: "
        f"{', '.join(f'#{n}' for n in prs)}[/bold]\n{'=' * 60}"
    )

    native_stack_number, dissolvable = _native_stack_for(prs, services)
    if native_stack_number is None:
        ui.print("[yellow]Landing these PRs one at a time instead.[/yellow]")
        return NativeStackMergeResult()

    failure = ""
    abort_reason = ""
    for entry in entries:
        if not waits.wait_for_approval(entry, opts=opts):
            abort_reason = (
                f"PR #{entry.pr_number} was closed while waiting for approval"
            )
            break
    else:
        for entry in entries:
            if entry.state == PRState.MERGED:
                continue
            if not waits.wait_for_checks(entry, opts=opts):
                abort_reason = f"PR #{entry.pr_number} checks failed after retries"
                break

    bottom = next((e for e in entries if e.state != PRState.MERGED), None)
    # Only the lowest open PR is checked here: GitHub reports the ones above it
    # as blocked until it merges. The merge request itself enforces every PR's
    # requirements.
    if (
        not abort_reason
        and bottom is not None
        and not waits.wait_for_mergeable(bottom, opts=opts).ready
    ):
        abort_reason = f"PR #{bottom.pr_number} failed to merge"

    if not abort_reason and bottom is not None:
        merging = (
            PRState.IN_MERGE_QUEUE if opts.merge_mode.uses_queue else PRState.MERGING
        )
        for entry in entries:
            if entry.state != PRState.MERGED:
                entry.state = merging
                entry.error_message = "Merging as a stack..."
        ui.print(
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
                github.set_base(ctx.stack[above].pr_number, common.target)
            except RuntimeError as e:
                failure = (
                    f"could not base PR #{ctx.stack[above].pr_number} on "
                    f"{common.target}: {e}"
                )
        if not failure:
            try:
                # The request body must match whether the branch really has a
                # queue (see merge_async). A direct run has found it has none,
                # or was told so. A queue run asks again: merge_queue = true
                # used to be required on any branch, queue or not.
                if opts.merge_mode.uses_queue:
                    has_queue = github.has_merge_queue(common.target)
                    use_queue = True if has_queue is None else has_queue
                else:
                    use_queue = False
                uuid = github.merge_async(top.pr_number, merge_queue=use_queue)
            except RuntimeError as e:
                # 409: a merge request for this stack is already in flight (e.g.
                # from a run that was interrupted) — wait for it like our own.
                if "HTTP 409" not in str(e):
                    failure = f"stack merge request failed: {e}"
        if not failure:
            failure = _await_native_stack_merge(
                entries, uuid, opts=opts, services=services
            )

    landed = all(e.state == PRState.MERGED for e in entries)
    if not landed and dissolvable:
        # Leave no stack behind: landing one PR at a time only works on PRs
        # that aren't in one. Queued PRs stay queued.
        try:
            github.unstack_native_stack(native_stack_number)
        except RuntimeError as e:
            ui.print(
                f"[yellow]Could not dissolve GitHub stack #{native_stack_number}: {e}[/yellow]"
            )

    merged = [e for e in entries if e.state == PRState.MERGED]
    if failure and not abort_reason:
        ui.print(
            f"[yellow]Stack merge did not complete ({failure}). Landing the "
            "remaining PRs one at a time.[/yellow]"
        )
        for entry in entries:
            if entry.state != PRState.MERGED:
                entry.state = PRState.PENDING
                entry.error_message = f"Stack merge: {failure}"
    elif landed:
        ui.print(
            f"\n[bold green]PRs {', '.join(f'#{n}' for n in prs)} merged![/bold green]"
        )

    if merged:
        landed_sha = repo.landed_sha(common, merged[-1].pr_number)
        if landed_sha:
            ctx.last_landed_sha = landed_sha
        if ctx.stack.index(merged[-1]) < len(ctx.stack) - 1:
            try:
                repo.rebase_and_resubmit(common)
            except Exception as e:  # noqa: BLE001 - report any resubmit failure
                return NativeStackMergeResult(
                    landed=landed,
                    abort_reason=(
                        f"Rebase failed after merging #{merged[-1].pr_number}: {e}"
                    ),
                )
    return NativeStackMergeResult(landed=landed, abort_reason=abort_reason)
