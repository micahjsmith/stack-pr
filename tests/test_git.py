import sys
from pathlib import Path

sys.path.append(str(Path(__file__).parent.parent / "src"))

from stack_pr.git import is_rebase_in_progress
from stack_pr.shell_commands import run_shell_command


def _git(cwd: Path, *args: str, check: bool = True) -> None:
    run_shell_command(["git", *args], cwd=cwd, check=check, quiet=True)


def _init_repo(repo: Path) -> None:
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.name", "Test User")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "file.txt").write_text("base\n")
    _git(repo, "add", "file.txt")
    _git(repo, "commit", "-q", "-m", "base")


def test_is_rebase_in_progress_in_linked_worktree(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    assert not is_rebase_in_progress(repo)

    # Diverge `main` and `feature` with conflicting edits to the same file.
    worktree = tmp_path / "worktree"
    _git(repo, "worktree", "add", "-q", "-b", "feature", str(worktree))
    (worktree / "file.txt").write_text("feature\n")
    _git(worktree, "commit", "-q", "-am", "feature")
    (repo / "file.txt").write_text("main\n")
    _git(repo, "commit", "-q", "-am", "main")

    assert (worktree / ".git").is_file()
    assert not is_rebase_in_progress(worktree)

    # The rebase stops on the conflict, leaving it in progress in the worktree.
    _git(worktree, "rebase", "main", check=False)

    assert is_rebase_in_progress(worktree)
    assert not is_rebase_in_progress(repo)
