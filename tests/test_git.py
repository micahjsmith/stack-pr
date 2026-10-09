from pathlib import Path

import pytest

from stack_pr.git import GitError, check_gh_installed, is_rebase_in_progress
from tests.helpers import git, init_repo


def test_is_rebase_in_progress_in_linked_worktree(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    assert not is_rebase_in_progress(repo)

    # Diverge `main` and `feature` with conflicting edits to the same file.
    worktree = tmp_path / "worktree"
    git(repo, "worktree", "add", "-q", "-b", "feature", str(worktree))
    (worktree / "file.txt").write_text("feature\n")
    git(worktree, "commit", "-q", "-am", "feature")
    (repo / "file.txt").write_text("main\n")
    git(repo, "commit", "-q", "-am", "main")

    assert (worktree / ".git").is_file()
    assert not is_rebase_in_progress(worktree)

    # The rebase stops on the conflict, leaving it in progress in the worktree.
    git(worktree, "rebase", "main", check=False)

    assert is_rebase_in_progress(worktree)
    assert not is_rebase_in_progress(repo)


def test_check_gh_installed_raises_git_error_when_gh_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(GitError, match="not installed"):
        check_gh_installed()
