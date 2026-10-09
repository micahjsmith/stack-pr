"""The temporary git worktree autoland lands from with ``--branch``."""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

from stack_pr.autoland import runtime
from stack_pr.shell_commands import run_shell_command

# ---------------------------------------------------------------------------
# Worktree management
# ---------------------------------------------------------------------------


class Worktree:
    """A temporary git worktree autoland operates in (for ``--branch``).

    ``create`` checks the branch out in a throwaway worktree and chdirs into
    it; ``remove`` restores the original directory and deletes the worktree.
    ``announce_preserved`` is used instead of ``remove`` to keep it around for
    debugging after a failure.

    Once landing starts (``landing`` is set), the run's outcome decides
    whether the worktree is kept (see ``_dispose_worktree``). Before then
    nothing worth debugging has happened, so ``remove_unless_landing`` deletes
    it however the setup ended.
    """

    def __init__(self, branch: str) -> None:
        self.branch = branch
        self.path: Path | None = None
        self.landing = False
        self._orig_cwd: str | None = None

    def remove_unless_landing(self) -> None:
        if not self.landing:
            self.remove()

    def create(self) -> None:
        tmpdir = tempfile.mkdtemp(prefix="autoland-")
        worktree_dir = str(Path(tmpdir) / "repo")
        runtime.console.print(
            f"[bold]Creating temporary worktree for [cyan]{self.branch}[/cyan] "
            f"at {worktree_dir}[/bold]"
        )
        try:
            run_shell_command(
                ["git", "worktree", "add", "-f", worktree_dir, self.branch],
                quiet=False,
                check=True,
                capture_output=True,
                text=True,
            )
        except BaseException:
            shutil.rmtree(tmpdir, ignore_errors=True)
            raise
        self.path = Path(worktree_dir)
        self._orig_cwd = str(Path.cwd())
        os.chdir(worktree_dir)

    def remove(self) -> None:
        if self.path is None:
            return
        if self._orig_cwd:
            os.chdir(self._orig_cwd)
            self._orig_cwd = None
        runtime.console.print(f"\n[dim]Cleaning up worktree at {self.path}...[/dim]")
        run_shell_command(
            ["git", "worktree", "remove", "--force", str(self.path)],
            quiet=False,
            check=False,
            capture_output=True,
            text=True,
        )
        shutil.rmtree(self.path.parent, ignore_errors=True)
        self.path = None

    def announce_preserved(self) -> None:
        if self.path is None:
            return
        runtime.console.print(
            f"\n[bold yellow]Worktree preserved at: "
            f"[cyan]{self.path}[/cyan][/bold yellow]"
        )
        runtime.console.print(
            f"[dim]To clean up manually: git worktree remove --force {self.path}[/dim]"
        )
