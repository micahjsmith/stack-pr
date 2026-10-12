"""Waiting for approval, checks, a merge (queued or direct), and workflows.

Every wait polls through the ``Services`` it is given: GitHub, the console,
the clock, and the local repo. ``services.PollingWaits`` binds them as the
production ``Waits``.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, TypeVar

from stack_pr import cli
from stack_pr.autoland.checks import CheckStatus, evaluate_checks
from stack_pr.autoland.display import _REVIEW_DECISION_DISPLAY
from stack_pr.autoland.gh import MergeQueuePollResult, _sha_eq
from stack_pr.autoland.model import LandingContext, PRState, StackEntry, WorkflowStep
from stack_pr.autoland.options import AutolandOptions

if TYPE_CHECKING:
    from stack_pr.autoland.services import Repo, Services

# ---------------------------------------------------------------------------
# Workflow checkpoint polling
# ---------------------------------------------------------------------------


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

    def __init__(self, common: cli.CommonArgs, services: Services) -> None:
        self._common = common
        self._repo: Repo = services.repo
        self._github = services.github
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

        verdict = self._repo.is_ancestor(ancestor, descendant)
        if verdict is None and descendant not in self._fetched_for:
            # A commit is missing locally. Fetch the target branch once for
            # this commit and retry; fetching again for the same commit would
            # not tell us anything new.
            self._fetched_for.add(descendant)
            self._repo.fetch_target(self._common)
            verdict = self._repo.is_ancestor(ancestor, descendant)
        if verdict is None:
            verdict = self._github.contains(ancestor, descendant)

        if verdict is not None:
            self._verdicts[key] = verdict
        return verdict


def wait_for_workflow(
    step: WorkflowStep,
    *,
    opts: AutolandOptions,
    common: cli.CommonArgs,
    ctx: LandingContext,
    target_sha: str | None = None,
    services: Services,
) -> bool:
    """Wait for a workflow to complete with code at or after *target_sha*.

    *target_sha* defaults to the run's last landed SHA.
    """
    ui, github = services.ui, services.github
    if target_sha is None:
        target_sha = ctx.last_landed_sha
    step.state = "waiting"
    ui.print(
        f"\n[bold blue]Waiting for workflow: {step.workflow}[/bold blue]"
        f"\n[dim]Target SHA: {target_sha[:12]}[/dim]"
    )

    ancestry = _Ancestry(common, services)
    awake_elapsed = 0.0
    while True:
        if awake_elapsed > opts.workflow_timeout:
            step.state = "failed"
            step.error_message = (
                f"Workflow timed out after {opts.workflow_timeout / 3600:.0f}h"
            )
            return False

        try:
            data = github.workflow_runs(step.workflow, common.target)
        except RuntimeError as e:
            ui.print(f"[yellow]Warning: could not poll workflow: {e}[/yellow]")
            services.sleep(opts.poll_interval)
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
                ui.print(
                    f"[yellow]Warning: could not tell whether run "
                    f"{run_sha[:12]} includes {target_sha[:12]}; "
                    "will retry[/yellow]"
                )
                continue
            if verdict:
                step.state = "succeeded"
                step.error_message = ""
                ui.print(
                    f"\n[bold green]Workflow {step.workflow} completed "
                    f"with SHA {run_sha[:12]}[/bold green]"
                )
                return True

        mins = int(awake_elapsed) // 60
        step.error_message = f"Waiting for workflow ({mins}m elapsed)..."
        ui.print(
            f"[dim]Workflow {step.workflow}: waiting ({mins}m) — "
            f"polling in {opts.poll_interval}s[/dim]"
        )
        services.sleep(opts.poll_interval)
        awake_elapsed += opts.poll_interval


# ---------------------------------------------------------------------------
# Landing logic
# ---------------------------------------------------------------------------


_T = TypeVar("_T")


def _poll_read(
    read: Callable[[int], _T], pr_number: int, services: Services
) -> _T | None:
    """One poll's read of a PR from GitHub, or ``None`` if it failed.

    A long wait shouldn't die on one bad response from GitHub, whether the
    ``gh`` call failed (after ``run``'s retries) or returned something
    malformed: the failure is reported, and the caller polls again on its next
    interval.
    """
    try:
        return read(pr_number)
    except RuntimeError as e:  # includes GitHubError
        services.ui.print(
            f"[yellow]Warning: could not poll PR #{pr_number}: {e}; will retry[/yellow]"
        )
        return None


def _refresh_review(entry: StackEntry, services: Services) -> bool:
    """Update the entry's review decision, tolerating a transient gh failure.

    Returns whether the entry is now approved.
    """
    with contextlib.suppress(RuntimeError):
        entry.review_decision = services.github.review_decision(entry.pr_number)
    return entry.is_approved


def wait_for_approval(
    entry: StackEntry, *, opts: AutolandOptions, services: Services
) -> bool:
    """Wait until the PR has required approvals (or merged). Returns False if
    the PR was closed."""
    ui, github = services.ui, services.github
    # An unknown state (a failed read) is settled by the checks wait after this.
    pr_state = _poll_read(github.pr_state, entry.pr_number, services)
    if pr_state == "MERGED":
        entry.state = PRState.MERGED
        return True
    if pr_state == "CLOSED":
        entry.state = PRState.FAILED
        entry.error_message = "PR was closed"
        return False

    if _refresh_review(entry, services):
        return True

    entry.state = PRState.WAITING_FOR_APPROVAL
    label, _ = _REVIEW_DECISION_DISPLAY.get(entry.review_decision, ("not approved", ""))
    entry.error_message = label
    ui.print(
        f"[magenta]PR #{entry.pr_number} is not yet approved "
        f"({entry.review_decision or 'REVIEW_REQUIRED'}). Waiting...[/magenta]"
    )

    while True:
        ui.print(
            f"[dim]PR #{entry.pr_number}: waiting for approval — "
            f"polling in {opts.poll_interval}s[/dim]"
        )
        services.sleep(opts.poll_interval)

        pr_state = _poll_read(github.pr_state, entry.pr_number, services)
        if pr_state is None:
            continue
        if pr_state == "MERGED":
            entry.state = PRState.MERGED
            return True
        if pr_state == "CLOSED":
            entry.state = PRState.FAILED
            entry.error_message = "PR was closed"
            return False

        if _refresh_review(entry, services):
            entry.error_message = ""
            ui.print(f"[green]PR #{entry.pr_number} is now approved[/green]")
            return True
        if entry.review_decision == "CHANGES_REQUESTED":
            entry.error_message = "Changes requested — cannot proceed"
            ui.print(
                f"[red]PR #{entry.pr_number} has changes requested. "
                "Resolve review comments and re-request review.[/red]"
            )


def wait_for_checks(
    entry: StackEntry, *, opts: AutolandOptions, services: Services
) -> bool:
    """Wait for required checks to pass. Returns True on success."""
    ui, github = services.ui, services.github
    entry.state = PRState.WAITING_FOR_CHECKS

    while True:
        pr_state = _poll_read(github.pr_state, entry.pr_number, services)
        if pr_state == "MERGED":
            entry.state = PRState.MERGED
            return True
        if pr_state == "CLOSED":
            entry.state = PRState.FAILED
            entry.error_message = "PR was closed"
            return False

        checks = (
            None
            if pr_state is None
            else _poll_read(github.checks, entry.pr_number, services)
        )
        if checks is None:
            services.sleep(opts.poll_interval)
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
            ui.print(
                f"[yellow]Rerunning failed checks "
                f"(attempt {entry.check_retries}/{opts.max_check_retries}): "
                f"{', '.join(result.failed_names)}[/yellow]"
            )
            github.rerun_failed(result.failed_runs)

        ui.print(
            f"[dim]PR #{entry.pr_number}: {result.summary} — "
            f"polling in {opts.poll_interval}s[/dim]"
        )
        services.sleep(opts.poll_interval)


_MERGEABLE_STATES = {"CLEAN", "UNSTABLE", "HAS_HOOKS"}


@dataclass
class MergeableResult:
    ready: bool = False
    already_merged: bool = False
    error: str = ""


def wait_for_mergeable(
    entry: StackEntry, *, opts: AutolandOptions, services: Services
) -> MergeableResult:
    """Wait until GitHub reports the PR as mergeable."""
    ui, github = services.ui, services.github
    uses_queue = opts.merge_mode.uses_queue
    reported_behind = False
    while True:
        data = _poll_read(github.merge_state, entry.pr_number, services)
        if data is None:
            services.sleep(opts.poll_interval)
            continue
        pr_state = data.get("state", "")
        merge_state = data.get("mergeStateStatus", "UNKNOWN")
        mergeable = data.get("mergeable", "UNKNOWN")

        if pr_state == "MERGED":
            ui.print(f"[green]PR #{entry.pr_number} is already merged[/green]")
            return MergeableResult(ready=True, already_merged=True)
        if pr_state == "CLOSED":
            entry.state = PRState.FAILED
            entry.error_message = "PR was closed"
            return MergeableResult(error="PR was closed")

        if merge_state in _MERGEABLE_STATES:
            ui.print(
                f"[green]PR #{entry.pr_number} is mergeable "
                f"(mergeStateStatus={merge_state})[/green]"
            )
            return MergeableResult(ready=True)

        if mergeable == "CONFLICTING":
            entry.error_message = "PR has merge conflicts — waiting for resolution"
            ui.print(
                f"\n[bold red]PR #{entry.pr_number} has merge conflicts! "
                "Resolve them on the PR branch and push; autoland will "
                "resume automatically.[/bold red]"
            )
            services.sleep(opts.poll_interval)
            continue

        # Without a queue, a branch that must be up to date with its base before
        # it merges stays BEHIND until someone updates it; autoland doesn't
        # push to a PR on its own, so it says what to do and waits.
        if not uses_queue and merge_state == "BEHIND":
            entry.error_message = "Branch is behind its base — waiting for an update"
            if not reported_behind:
                reported_behind = True
                ui.print(
                    f"\n[bold yellow]PR #{entry.pr_number} is behind its base "
                    "branch, which must be up to date before it merges. Rebase "
                    "the stack and run `stack-pr submit` (or update the branch "
                    "on GitHub); autoland will resume automatically.[/bold yellow]"
                )
            ui.print(
                f"[dim]PR #{entry.pr_number}: behind its base — "
                f"polling in {opts.poll_interval}s[/dim]"
            )
            services.sleep(opts.poll_interval)
            continue

        # UNKNOWN can also mean "already in the merge queue".
        if (
            uses_queue
            and merge_state == "UNKNOWN"
            and github.in_merge_queue(entry.pr_number) is True
        ):
            ui.print(
                f"[cyan]PR #{entry.pr_number} is already in the merge queue — "
                "skipping enqueue[/cyan]"
            )
            return MergeableResult(ready=True, already_merged=False)

        entry.error_message = f"Waiting for mergeable state (currently {merge_state})"
        ui.print(
            f"[dim]PR #{entry.pr_number}: mergeStateStatus={merge_state} — "
            f"polling in {opts.poll_interval}s[/dim]"
        )
        services.sleep(opts.poll_interval)


def _rewait_after_retry(
    entry: StackEntry, *, opts: AutolandOptions, services: Services
) -> bool:
    """Re-verify approval and checks before retrying a merge."""
    if not wait_for_approval(entry, opts=opts, services=services):
        return False
    entry.check_retries = 0
    return wait_for_checks(entry, opts=opts, services=services)


def enqueue_and_wait(
    entry: StackEntry, *, opts: AutolandOptions, services: Services
) -> bool:
    """Add the PR to the merge queue and wait for it to merge."""
    ui, github = services.ui, services.github
    while True:
        mergeable_result = wait_for_mergeable(entry, opts=opts, services=services)
        if not mergeable_result.ready:
            return False
        if mergeable_result.already_merged:
            entry.state = PRState.MERGED
            entry.error_message = ""
            ui.print(
                f"\n[bold green]PR #{entry.pr_number} already merged![/bold green]"
            )
            return True

        entry.state = PRState.IN_MERGE_QUEUE
        entry.error_message = "Adding to merge queue..."
        ui.print(
            f"\n[bold cyan]Adding PR #{entry.pr_number} to merge queue[/bold cyan]"
        )

        try:
            github.enqueue(entry.pr_number)
        except RuntimeError as e:
            entry.error_message = f"Failed to enqueue: {e}"
            ui.print(f"[red]Failed to add to merge queue: {e}[/red]")
            if entry.queue_retries >= opts.max_queue_retries:
                entry.state = PRState.FAILED
                entry.error_message = (
                    f"Failed to enqueue after {opts.max_queue_retries} attempts"
                )
                return False
            entry.queue_retries += 1
            if not _rewait_after_retry(entry, opts=opts, services=services):
                return False
            continue

        entry.error_message = "Waiting in merge queue..."
        awake_elapsed = 0.0
        while True:
            if awake_elapsed > opts.merge_timeout:
                entry.state = PRState.FAILED
                entry.error_message = "Timed out waiting for merge queue"
                return False

            # A failed read leaves the merge undecided; poll again.
            poll = (
                _poll_read(github.poll_merge, entry.pr_number, services)
                or MergeQueuePollResult()
            )
            if poll.merged:
                entry.state = PRState.MERGED
                entry.error_message = ""
                ui.print(f"\n[bold green]PR #{entry.pr_number} merged![/bold green]")
                return True
            if poll.error:
                entry.state = PRState.FAILED
                entry.error_message = poll.error
                return False
            if poll.booted:
                ui.print(
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
                if not _rewait_after_retry(entry, opts=opts, services=services):
                    return False
                break  # re-enqueue in outer loop

            mins = int(awake_elapsed) // 60
            entry.error_message = f"In merge queue ({mins}m elapsed)..."
            ui.print(
                f"[dim]PR #{entry.pr_number}: in merge queue ({mins}m) — "
                f"polling in {opts.poll_interval}s[/dim]"
            )
            services.sleep(opts.poll_interval)
            awake_elapsed += opts.poll_interval


def merge_and_wait(
    entry: StackEntry, *, opts: AutolandOptions, services: Services
) -> bool:
    """Merge the PR the way the run merges (``opts.merge_mode``), and wait until
    it has merged."""
    if opts.merge_mode.uses_queue:
        return enqueue_and_wait(entry, opts=opts, services=services)
    return merge_directly(entry, opts=opts, services=services)


# After a direct merge, how many times (and how many seconds apart) to read the
# PR's state before deciding GitHub didn't merge it after all. GitHub usually
# reports the merge at once; this only covers it lagging behind.
_MERGE_CONFIRM_READS = 6
_MERGE_CONFIRM_INTERVAL = 5


def _squash_merge(entry: StackEntry, services: Services) -> None:
    """Squash-merge the PR as ``stack-pr land`` would: as its commit message
    without the stack-info trailer, with the PR number in the subject.

    Pinned to the head commit the message was read from, so a push since then
    fails the merge instead of landing code under another commit's message.
    """
    github = services.github
    head = github.head_sha(entry.pr_number)
    subject, body = cli.squash_commit_message(
        github.commit_message(head), entry.pr_number
    )
    github.squash_merge(entry.pr_number, subject=subject, body=body, head_sha=head)


def _confirm_merged(entry: StackEntry, *, reads: int, services: Services) -> str:
    """The PR's state after a merge attempt: "MERGED" or "CLOSED" as soon as
    GitHub reports it, else what the last of *reads* reads said ("" if it
    failed)."""
    state = ""
    for attempt in range(reads):
        if attempt:
            services.sleep(_MERGE_CONFIRM_INTERVAL)
        state = _poll_read(services.github.pr_state, entry.pr_number, services) or ""
        if state in ("MERGED", "CLOSED"):
            break
    return state


def merge_directly(
    entry: StackEntry, *, opts: AutolandOptions, services: Services
) -> bool:
    """Squash-merge the PR straight into its base branch, with no merge queue.

    Waits for it to be mergeable first. A merge that fails, or that GitHub
    doesn't report as merged, is retried up to ``max_queue_retries`` times,
    after approval and checks are verified again.
    """
    ui = services.ui
    while True:
        mergeable_result = wait_for_mergeable(entry, opts=opts, services=services)
        if not mergeable_result.ready:
            return False
        if mergeable_result.already_merged:
            entry.state = PRState.MERGED
            entry.error_message = ""
            ui.print(
                f"\n[bold green]PR #{entry.pr_number} already merged![/bold green]"
            )
            return True

        entry.state = PRState.MERGING
        entry.error_message = "Merging..."
        ui.print(f"\n[bold cyan]Merging PR #{entry.pr_number}[/bold cyan]")

        try:
            _squash_merge(entry, services)
        except RuntimeError as e:
            failure = str(e)
            # The merge may have gone through even though gh reported an error
            # (e.g. a lost response), so look before retrying.
            reads = 1
        else:
            failure = "GitHub did not report the PR as merged"
            reads = _MERGE_CONFIRM_READS

        state = _confirm_merged(entry, reads=reads, services=services)
        if state == "MERGED":
            entry.state = PRState.MERGED
            entry.error_message = ""
            ui.print(f"\n[bold green]PR #{entry.pr_number} merged![/bold green]")
            return True
        if state == "CLOSED":
            entry.state = PRState.FAILED
            entry.error_message = "PR was closed"
            return False

        entry.error_message = f"Failed to merge: {failure}"
        ui.print(f"[red]Failed to merge PR #{entry.pr_number}: {failure}[/red]")
        if entry.queue_retries >= opts.max_queue_retries:
            entry.state = PRState.FAILED
            entry.error_message = (
                f"Failed to merge after {opts.max_queue_retries + 1} attempts: "
                f"{failure}"
            )
            return False
        entry.queue_retries += 1
        ui.print(
            f"[yellow]Retrying the merge of PR #{entry.pr_number} "
            f"(attempt {entry.queue_retries + 1}/{opts.max_queue_retries + 1})"
            "[/yellow]"
        )
        if not _rewait_after_retry(entry, opts=opts, services=services):
            return False
