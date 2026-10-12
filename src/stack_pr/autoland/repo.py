"""The local clone: fetching, ancestry, landed SHAs, rebase + resubmit, and
where it is.

``GitRepo`` is the production ``services.Repo``. It runs git in the current
working directory (autoland chdirs into a temporary worktree when ``--branch``
is used) and stack-pr's own submit for the resubmit.
"""

from __future__ import annotations

import contextlib
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from stack_pr import cli
from stack_pr.autoland import runtime
from stack_pr.git import GitError, is_ancestor

if TYPE_CHECKING:
    from stack_pr.autoland import gh
    from stack_pr.autoland.services import UI


class GitRepo:
    """The clone autoland runs in, and the stack-pr operations on it."""

    def __init__(self, github: gh.GitHub, ui: UI) -> None:
        self._github = github
        self._ui = ui

    def fetch_target(self, common: cli.CommonArgs) -> None:
        """Fetch ``<remote>/<target>``. Best-effort: a failure is ignored."""
        with contextlib.suppress(RuntimeError):  # fetch is non-critical
            runtime.run(["git", "fetch", common.remote, common.target], quiet=True)

    def is_ancestor(self, ancestor: str, descendant: str) -> bool | None:
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

    def landed_sha(self, common: cli.CommonArgs, pr_number: int | None = None) -> str:
        """The SHA of the most recently landed code, or "" if it can't be found.

        When ``pr_number`` is given, prefer that PR's exact merge commit so the
        workflow checkpoint targets the code we actually landed. In a busy
        repo, ``origin/<target>`` can advance past our merge commit (bot
        commits, other PRs) between the merge and this call, so using its HEAD
        would overshoot and make the checkpoint wait for a deploy of code that
        was never our own.
        """
        self.fetch_target(common)

        if pr_number is not None:
            merge_sha = self._github.merge_commit(pr_number)
            if merge_sha:
                return merge_sha

        try:
            result = runtime.run(
                ["git", "rev-parse", f"{common.remote}/{common.target}"], quiet=True
            )
        except RuntimeError:
            return ""  # non-critical; will retry when needed
        return result.stdout.strip()

    def rebase_and_resubmit(self, common: cli.CommonArgs) -> None:
        """After a merge, rebase the local stack on the target and re-submit."""
        self._ui.print(
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
            # Without --branch this is the user's own working copy; don't leave
            # it stuck mid-rebase. Best-effort: `--abort` is harmless (and
            # fails quietly) if no rebase is actually in progress.
            runtime.run(
                ["git", "rebase", "--abort"], check=False, quiet=True, retries=0
            )
            raise

        # Re-deduce the base against the *current* origin/<target>.
        # `common.base` was deduced once when autoland started (merge-base with
        # the target at that time). After other PRs land in the target and we
        # rebase onto it, that cached base is stale: the range base..HEAD would
        # then sweep in every commit merged by others in the meantime, and
        # submit would try to open bogus PRs for them. Clearing the base forces
        # deduce_base to recompute merge-base(HEAD, origin/<target>) from the
        # freshly rebased HEAD.
        resubmit_common = cli.deduce_base(replace(common, base=""))

        self._ui.print("[bold]Re-submitting stack...[/bold]")
        cli.command_submit(
            resubmit_common,
            draft=False,
            reviewer="",
            keep_body=True,
            keep_title=True,
            draft_bitmask=None,
        )

    def root(self) -> Path:
        """The top-level directory of the checkout autoland runs in."""
        result = runtime.run(["git", "rev-parse", "--show-toplevel"], quiet=True)
        return Path(result.stdout.strip())
