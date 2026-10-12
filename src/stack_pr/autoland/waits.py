"""Waiting for approval, checks, the merge queue, and workflows; rebase + resubmit."""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import TypeVar

from stack_pr import cli
from stack_pr.autoland import gh, runtime
from stack_pr.autoland.checks import CheckStatus, evaluate_checks
from stack_pr.autoland.display import _REVIEW_DECISION_DISPLAY
from stack_pr.autoland.gh import MergeQueuePollResult, _sha_eq
from stack_pr.autoland.model import LandingContext, PRState, StackEntry, WorkflowStep
from stack_pr.autoland.options import AutolandOptions
from stack_pr.git import GitError, is_ancestor

# ---------------------------------------------------------------------------
# Post-merge rebase + resubmit (reuses stack-pr submit)
# ---------------------------------------------------------------------------


def rebase_and_resubmit(common: cli.CommonArgs) -> None:
    """After a merge, rebase the local stack on the target and re-submit."""
    runtime.console.print(
        f"\n[bold]Rebasing stack on {common.remote}/{common.target}...[/bold]"
    )
    runtime.run(["git", "fetch", common.remote, common.target], quiet=False)
    # Rebase the current branch (don't name it) so this works even when the
    # branch is checked out in another worktree.
    try:
        runtime.run(
            ["git", "rebase", f"{common.remote}/{common.target}"],
            quiet=False,
            retries=0,
        )
    except RuntimeError:
        # Without --branch this is the user's own working copy; don't leave it
        # stuck mid-rebase. Best-effort: `--abort` is harmless (and fails
        # quietly) if no rebase is actually in progress.
        runtime.run(["git", "rebase", "--abort"], check=False, quiet=True, retries=0)
        raise

    # Re-deduce the base against the *current* origin/<target>. `common.base`
    # was deduced once when autoland started (merge-base with the target at
    # that time). After other PRs land in the target and we rebase onto it,
    # that cached base is stale: the range base..HEAD would then sweep in every
    # commit merged by others in the meantime, and submit would try to open
    # bogus PRs for them. Clearing the base forces deduce_base to recompute
    # merge-base(HEAD, origin/<target>) from the freshly rebased HEAD.
    resubmit_common = cli.deduce_base(replace(common, base=""))

    runtime.console.print("[bold]Re-submitting stack...[/bold]")
    cli.command_submit(
        resubmit_common,
        draft=False,
        reviewer="",
        keep_body=True,
        keep_title=True,
        draft_bitmask=None,
    )


# ---------------------------------------------------------------------------
# Workflow checkpoint polling
# ---------------------------------------------------------------------------


def _refresh_last_landed_sha(
    ctx: LandingContext, common: cli.CommonArgs, pr_number: int | None = None
) -> None:
    """Record the SHA of the most recently landed code.

    When ``pr_number`` is given, prefer that PR's exact merge commit so the
    workflow checkpoint targets the code we actually landed. In a busy repo,
    ``origin/<target>`` can advance past our merge commit (bot commits, other
    PRs) between the merge and this call, so using its HEAD would overshoot and
    make the checkpoint wait for a deploy of code that was never our own.
    """
    with contextlib.suppress(RuntimeError):  # fetch is non-critical
        runtime.run(["git", "fetch", common.remote, common.target], quiet=True)

    if pr_number is not None:
        merge_sha = gh.github.merge_commit(pr_number)
        if merge_sha:
            ctx.last_landed_sha = merge_sha
            return

    try:
        result = runtime.run(
            ["git", "rev-parse", f"{common.remote}/{common.target}"], quiet=True
        )
        ctx.last_landed_sha = result.stdout.strip()
    except RuntimeError:
        pass  # non-critical; will retry when needed


def _local_is_ancestor(ancestor: str, descendant: str) -> bool | None:
    """Whether ``ancestor`` is an ancestor of ``descendant``, per this clone.

    ``None`` means the local repo cannot answer, typically because either
    commit is missing from this clone. That must not be conflated with a
    genuine "no" — a workflow run's head commit is routinely absent here,
    because ``origin/<target>`` advances while we poll.
    """
    try:
        return is_ancestor(ancestor, descendant)
    except GitError:
        return None


class _Ancestry:
    """Answers "does this workflow run's commit include the code we landed?".

    Tries the local repo first, then a fetch of the target branch, then the
    GitHub compare API. The fetch is what usually resolves it: the run's head
    commit is a commit on ``<remote>/<target>`` that this clone has not seen
    yet. The API call covers the rest (e.g. a merge-queue commit that never
    landed on the target branch, or a clone that cannot fetch).

    Commits are immutable, so a definitive verdict is cached for the rest of
    the wait; the poll loop re-examines the same runs every interval.
    """

    def __init__(self, common: cli.CommonArgs) -> None:
        self._common = common
        self._verdicts: dict[tuple[str, str], bool] = {}
        self._fetched_for: set[str] = set()

    def contains(self, ancestor: str, descendant: str) -> bool | None:
        """Whether ``descendant`` is ``ancestor`` or a commit after it.

        ``None`` if neither git nor GitHub could tell us.
        """
        if _sha_eq(ancestor, descendant):
            return True
        key = (ancestor, descendant)
        if key in self._verdicts:
            return self._verdicts[key]

        verdict = _local_is_ancestor(ancestor, descendant)
        if verdict is None and descendant not in self._fetched_for:
            # A commit is missing locally. Fetch the target branch once for
            # this commit and retry; fetching again for the same commit would
            # not tell us anything new.
            self._fetched_for.add(descendant)
            self._fetch_target()
            verdict = _local_is_ancestor(ancestor, descendant)
        if verdict is None:
            verdict = gh.github.contains(ancestor, descendant)

        if verdict is not None:
            self._verdicts[key] = verdict
        return verdict

    def _fetch_target(self) -> None:
        with contextlib.suppress(RuntimeError):  # fetch is non-critical
            runtime.run(
                ["git", "fetch", self._common.remote, self._common.target],
                quiet=True,
            )


def wait_for_workflow(
    step: WorkflowStep,
    *,
    opts: AutolandOptions,
    common: cli.CommonArgs,
    ctx: LandingContext,
) -> bool:
    """Wait for a workflow to complete with code at or after the landed SHA."""
    target_sha = ctx.last_landed_sha
    step.state = "waiting"
    runtime.console.print(
        f"\n[bold blue]Waiting for workflow: {step.workflow}[/bold blue]"
        f"\n[dim]Target SHA: {target_sha[:12]}[/dim]"
    )

    ancestry = _Ancestry(common)
    awake_elapsed = 0.0
    while True:
        if ctx.aborted:
            return False
        if awake_elapsed > opts.workflow_timeout:
            step.state = "failed"
            step.error_message = (
                f"Workflow timed out after {opts.workflow_timeout / 3600:.0f}h"
            )
            return False

        try:
            data = gh.github.workflow_runs(step.workflow, common.target)
        except RuntimeError as e:
            runtime.console.print(
                f"[yellow]Warning: could not poll workflow: {e}[/yellow]"
            )
            runtime.resilient_sleep(opts.poll_interval)
            awake_elapsed += opts.poll_interval
            continue

        for wf_run in data:
            if wf_run.get("status") != "completed":
                continue
            if wf_run.get("conclusion") != "success":
                continue
            run_sha = wf_run.get("headSha", "")
            if not run_sha:
                continue
            verdict = ancestry.contains(target_sha, run_sha)
            if verdict is None:
                runtime.console.print(
                    f"[yellow]Warning: could not tell whether run "
                    f"{run_sha[:12]} includes {target_sha[:12]}; "
                    "will retry[/yellow]"
                )
                continue
            if verdict:
                step.state = "succeeded"
                step.error_message = ""
                runtime.console.print(
                    f"\n[bold green]Workflow {step.workflow} completed "
                    f"with SHA {run_sha[:12]}[/bold green]"
                )
                return True

        mins = int(awake_elapsed) // 60
        step.error_message = f"Waiting for workflow ({mins}m elapsed)..."
        runtime.console.print(
            f"[dim]Workflow {step.workflow}: waiting ({mins}m) — "
            f"polling in {opts.poll_interval}s[/dim]"
        )
        runtime.resilient_sleep(opts.poll_interval)
        awake_elapsed += opts.poll_interval


# ---------------------------------------------------------------------------
# Landing logic
# ---------------------------------------------------------------------------


_T = TypeVar("_T")


def _poll_read(read: Callable[[int], _T], pr_number: int) -> _T | None:
    """One poll's read of a PR from GitHub, or ``None`` if it failed.

    A long wait shouldn't die on one bad response from GitHub, whether the
    ``gh`` call failed (after ``run``'s retries) or returned something
    malformed: the failure is reported, and the caller polls again on its next
    interval.
    """
    try:
        return read(pr_number)
    except RuntimeError as e:  # includes GitHubError
        runtime.console.print(
            f"[yellow]Warning: could not poll PR #{pr_number}: {e}; will retry[/yellow]"
        )
        return None


def _refresh_review(entry: StackEntry) -> bool:
    """Update the entry's review decision, tolerating a transient gh failure.

    Returns whether the entry is now approved.
    """
    with contextlib.suppress(RuntimeError):
        entry.review_decision = gh.github.review_decision(entry.pr_number)
    return entry.is_approved


def wait_for_approval(
    entry: StackEntry, *, opts: AutolandOptions, ctx: LandingContext
) -> bool:
    """Wait until the PR has required approvals. Returns False if aborted."""
    # An unknown state (a failed read) is settled by the checks wait after this.
    pr_state = _poll_read(gh.github.pr_state, entry.pr_number)
    if pr_state == "MERGED":
        entry.state = PRState.MERGED
        return True
    if pr_state == "CLOSED":
        entry.state = PRState.FAILED
        entry.error_message = "PR was closed"
        return False

    if _refresh_review(entry):
        return True

    entry.state = PRState.WAITING_FOR_APPROVAL
    label, _ = _REVIEW_DECISION_DISPLAY.get(entry.review_decision, ("not approved", ""))
    entry.error_message = label
    runtime.console.print(
        f"[magenta]PR #{entry.pr_number} is not yet approved "
        f"({entry.review_decision or 'REVIEW_REQUIRED'}). Waiting...[/magenta]"
    )

    while True:
        if ctx.aborted:
            return False
        runtime.console.print(
            f"[dim]PR #{entry.pr_number}: waiting for approval — "
            f"polling in {opts.poll_interval}s[/dim]"
        )
        runtime.resilient_sleep(opts.poll_interval)

        pr_state = _poll_read(gh.github.pr_state, entry.pr_number)
        if pr_state is None:
            continue
        if pr_state == "MERGED":
            entry.state = PRState.MERGED
            return True
        if pr_state == "CLOSED":
            entry.state = PRState.FAILED
            entry.error_message = "PR was closed"
            return False

        if _refresh_review(entry):
            entry.error_message = ""
            runtime.console.print(
                f"[green]PR #{entry.pr_number} is now approved[/green]"
            )
            return True
        if entry.review_decision == "CHANGES_REQUESTED":
            entry.error_message = "Changes requested — cannot proceed"
            runtime.console.print(
                f"[red]PR #{entry.pr_number} has changes requested. "
                "Resolve review comments and re-request review.[/red]"
            )


def wait_for_checks(
    entry: StackEntry, *, opts: AutolandOptions, ctx: LandingContext
) -> bool:
    """Wait for required checks to pass. Returns True on success."""
    entry.state = PRState.WAITING_FOR_CHECKS

    while True:
        if ctx.aborted:
            return False

        pr_state = _poll_read(gh.github.pr_state, entry.pr_number)
        if pr_state == "MERGED":
            entry.state = PRState.MERGED
            return True
        if pr_state == "CLOSED":
            entry.state = PRState.FAILED
            entry.error_message = "PR was closed"
            return False

        checks = (
            None if pr_state is None else _poll_read(gh.github.checks, entry.pr_number)
        )
        if checks is None:
            runtime.resilient_sleep(opts.poll_interval)
            continue
        result = evaluate_checks(checks, opts.required_checks)
        entry.error_message = result.summary

        if result.status == CheckStatus.ALL_PASSING:
            entry.error_message = "All checks passing"
            return True

        if result.status == CheckStatus.FAILED:
            if entry.check_retries >= opts.max_check_retries:
                entry.state = PRState.FAILED
                entry.error_message = (
                    f"Checks failed after {opts.max_check_retries} retries: "
                    f"{', '.join(result.failed_names)}"
                )
                return False
            entry.check_retries += 1
            runtime.console.print(
                f"[yellow]Rerunning failed checks "
                f"(attempt {entry.check_retries}/{opts.max_check_retries}): "
                f"{', '.join(result.failed_names)}[/yellow]"
            )
            gh.github.rerun_failed(result.failed_runs)

        runtime.console.print(
            f"[dim]PR #{entry.pr_number}: {result.summary} — "
            f"polling in {opts.poll_interval}s[/dim]"
        )
        runtime.resilient_sleep(opts.poll_interval)


_MERGEABLE_STATES = {"CLEAN", "UNSTABLE", "HAS_HOOKS"}


@dataclass
class MergeableResult:
    ready: bool = False
    already_merged: bool = False
    error: str = ""


def wait_for_mergeable(
    entry: StackEntry, *, opts: AutolandOptions, ctx: LandingContext
) -> MergeableResult:
    """Wait until GitHub reports the PR as mergeable."""
    while True:
        if ctx.aborted:
            return MergeableResult(error="aborted")

        data = _poll_read(gh.github.merge_state, entry.pr_number)
        if data is None:
            runtime.resilient_sleep(opts.poll_interval)
            continue
        pr_state = data.get("state", "")
        merge_state = data.get("mergeStateStatus", "UNKNOWN")
        mergeable = data.get("mergeable", "UNKNOWN")

        if pr_state == "MERGED":
            runtime.console.print(
                f"[green]PR #{entry.pr_number} is already merged[/green]"
            )
            return MergeableResult(ready=True, already_merged=True)
        if pr_state == "CLOSED":
            entry.state = PRState.FAILED
            entry.error_message = "PR was closed"
            return MergeableResult(error="PR was closed")

        if merge_state in _MERGEABLE_STATES:
            runtime.console.print(
                f"[green]PR #{entry.pr_number} is mergeable "
                f"(mergeStateStatus={merge_state})[/green]"
            )
            return MergeableResult(ready=True)

        if mergeable == "CONFLICTING":
            entry.error_message = "PR has merge conflicts — waiting for resolution"
            runtime.console.print(
                f"\n[bold red]PR #{entry.pr_number} has merge conflicts! "
                "Resolve them on the PR branch and push; autoland will "
                "resume automatically.[/bold red]"
            )
            runtime.resilient_sleep(opts.poll_interval)
            continue

        # UNKNOWN can also mean "already in the merge queue".
        if (
            merge_state == "UNKNOWN"
            and gh.github.in_merge_queue(entry.pr_number) is True
        ):
            runtime.console.print(
                f"[cyan]PR #{entry.pr_number} is already in the merge queue — "
                "skipping enqueue[/cyan]"
            )
            return MergeableResult(ready=True, already_merged=False)

        entry.error_message = f"Waiting for mergeable state (currently {merge_state})"
        runtime.console.print(
            f"[dim]PR #{entry.pr_number}: mergeStateStatus={merge_state} — "
            f"polling in {opts.poll_interval}s[/dim]"
        )
        runtime.resilient_sleep(opts.poll_interval)


def _rewait_after_retry(
    entry: StackEntry, *, opts: AutolandOptions, ctx: LandingContext
) -> bool:
    """Re-verify approval and checks before a queue retry."""
    if not wait_for_approval(entry, opts=opts, ctx=ctx):
        return False
    entry.check_retries = 0
    return wait_for_checks(entry, opts=opts, ctx=ctx)


def enqueue_and_wait(
    entry: StackEntry, *, opts: AutolandOptions, ctx: LandingContext
) -> bool:
    """Add the PR to the merge queue and wait for it to merge."""
    while True:
        if ctx.aborted:
            return False

        mergeable_result = wait_for_mergeable(entry, opts=opts, ctx=ctx)
        if not mergeable_result.ready:
            return False
        if mergeable_result.already_merged:
            entry.state = PRState.MERGED
            entry.error_message = ""
            runtime.console.print(
                f"\n[bold green]PR #{entry.pr_number} already merged![/bold green]"
            )
            return True

        entry.state = PRState.IN_MERGE_QUEUE
        entry.error_message = "Adding to merge queue..."
        runtime.console.print(
            f"\n[bold cyan]Adding PR #{entry.pr_number} to merge queue[/bold cyan]"
        )

        try:
            gh.github.enqueue(entry.pr_number)
        except RuntimeError as e:
            entry.error_message = f"Failed to enqueue: {e}"
            runtime.console.print(f"[red]Failed to add to merge queue: {e}[/red]")
            if entry.queue_retries >= opts.max_queue_retries:
                entry.state = PRState.FAILED
                entry.error_message = (
                    f"Failed to enqueue after {opts.max_queue_retries} attempts"
                )
                return False
            entry.queue_retries += 1
            if not _rewait_after_retry(entry, opts=opts, ctx=ctx):
                return False
            continue

        entry.error_message = "Waiting in merge queue..."
        awake_elapsed = 0.0
        while True:
            if ctx.abort_requested():
                return False
            if awake_elapsed > opts.merge_timeout:
                entry.state = PRState.FAILED
                entry.error_message = "Timed out waiting for merge queue"
                return False

            # A failed read leaves the merge undecided; poll again.
            poll = (
                _poll_read(gh.github.poll_merge, entry.pr_number)
                or MergeQueuePollResult()
            )
            if poll.merged:
                entry.state = PRState.MERGED
                entry.error_message = ""
                runtime.console.print(
                    f"\n[bold green]PR #{entry.pr_number} merged![/bold green]"
                )
                return True
            if poll.error:
                entry.state = PRState.FAILED
                entry.error_message = poll.error
                return False
            if poll.booted:
                runtime.console.print(
                    f"\n[yellow]PR #{entry.pr_number} was booted from the "
                    "merge queue[/yellow]"
                )
                if entry.queue_retries >= opts.max_queue_retries:
                    entry.state = PRState.FAILED
                    entry.error_message = (
                        f"Booted from queue {opts.max_queue_retries} times, giving up"
                    )
                    return False
                entry.queue_retries += 1
                if not _rewait_after_retry(entry, opts=opts, ctx=ctx):
                    return False
                break  # re-enqueue in outer loop

            mins = int(awake_elapsed) // 60
            entry.error_message = f"In merge queue ({mins}m elapsed)..."
            runtime.console.print(
                f"[dim]PR #{entry.pr_number}: in merge queue ({mins}m) — "
                f"polling in {opts.poll_interval}s[/dim]"
            )
            runtime.resilient_sleep(opts.poll_interval)
            awake_elapsed += opts.poll_interval
