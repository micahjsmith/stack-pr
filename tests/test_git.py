from pathlib import Path

import pytest

from stack_pr import git as stack_git
from stack_pr.cli import is_repo_clean
from stack_pr.git import (
    GitError,
    branch_exists,
    check_gh_installed,
    get_changed_dirs,
    get_changed_files,
    get_current_branch_name,
    get_gh_username,
    get_repo_root,
    get_uncommitted_changes,
    is_full_git_sha,
    is_rebase_in_progress,
)
from tests.helpers import git, init_repo


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return init_repo(tmp_path / "repo")


@pytest.fixture
def not_a_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "plain"
    path.mkdir()
    # Keep git from finding an enclosing repo (e.g. when tmp is inside one).
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    return path


def test_branch_exists(repo: Path) -> None:
    git(repo, "branch", "feature")
    git(repo, "tag", "a-tag")

    assert branch_exists("main", repo)
    assert branch_exists("feature", repo)
    assert not branch_exists("missing", repo)
    # Only local branches count, not tags.
    assert not branch_exists("a-tag", repo)


def test_branch_exists_outside_a_repo(not_a_repo: Path) -> None:
    with pytest.raises(GitError):
        branch_exists("main", not_a_repo)


def test_get_current_branch_name(repo: Path) -> None:
    assert get_current_branch_name(repo) == "main"
    git(repo, "checkout", "-q", "--detach")
    assert get_current_branch_name(repo) == "HEAD"


def test_get_current_branch_name_outside_a_repo(not_a_repo: Path) -> None:
    with pytest.raises(GitError):
        get_current_branch_name(not_a_repo)


def test_get_repo_root_from_a_subdirectory(repo: Path) -> None:
    sub = repo / "a" / "b"
    sub.mkdir(parents=True)
    assert get_repo_root(sub).resolve() == repo.resolve()


def test_get_repo_root_outside_a_repo(not_a_repo: Path) -> None:
    with pytest.raises(GitError):
        get_repo_root(not_a_repo)


def test_get_uncommitted_changes_groups_paths_by_status(repo: Path) -> None:
    assert get_uncommitted_changes(repo) == {}

    (repo / "file.txt").write_text("modified\n")
    (repo / "staged.txt").write_text("new\n")
    git(repo, "add", "staged.txt")
    (repo / "untracked.txt").write_text("?\n")
    (repo / "other.txt").write_text("?\n")

    assert get_uncommitted_changes(repo) == {
        " M": ["file.txt"],
        "A ": ["staged.txt"],
        "??": ["other.txt", "untracked.txt"],
    }

    git(repo, "reset", "-q", "--hard")
    git(repo, "mv", "file.txt", "moved.txt")
    assert get_uncommitted_changes(repo)["R "] == ["file.txt -> moved.txt"]


def test_get_uncommitted_changes_outside_a_repo(not_a_repo: Path) -> None:
    with pytest.raises(GitError):
        get_uncommitted_changes(not_a_repo)


def test_is_repo_clean_ignores_untracked_files(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(repo)
    (repo / "untracked.txt").write_text("?\n")
    assert is_repo_clean()

    (repo / "file.txt").write_text("modified\n")
    assert not is_repo_clean()


def test_get_changed_files_and_dirs(repo: Path) -> None:
    git(repo, "checkout", "-q", "-b", "feature")
    (repo / "src").mkdir()
    (repo / "src" / "a.py").write_text("a\n")
    (repo / "file.txt").write_text("changed\n")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "change")
    # Uncommitted edits are not part of the diff to HEAD.
    (repo / "dirty.txt").write_text("x\n")

    assert sorted(get_changed_files(repo_dir=repo)) == [
        Path("file.txt"),
        Path("src/a.py"),
    ]
    assert get_changed_dirs(repo_dir=repo) == {Path("file.txt"), Path("src")}
    # An explicit base works like the default 'main'.
    assert get_changed_files("HEAD~1", repo_dir=repo) == get_changed_files(
        repo_dir=repo
    )


def test_get_changed_files_with_no_changes(repo: Path) -> None:
    assert get_changed_files(repo_dir=repo) == []


def test_get_changed_dirs_with_no_changes(repo: Path) -> None:
    assert get_changed_dirs(repo_dir=repo) == set()


@pytest.mark.parametrize(
    ("s", "expected"),
    [
        ("0123456789abcdef0123456789abcdef01234567", True),
        ("0123456789ABCDEF0123456789ABCDEF01234567", False),
        ("0123456789abcdef", False),
        ("0123456789abcdef0123456789abcdef0123456g", False),
        ("", False),
    ],
)
def test_is_full_git_sha(s: str, expected: bool) -> None:
    assert is_full_git_sha(s) is expected


def test_get_gh_username_reads_the_viewer_login(mocker) -> None:  # noqa: ANN001
    mocker.patch.object(
        stack_git,
        "get_command_output",
        return_value='{"data":{"viewer":{"login":"octocat"}}}',
    )
    assert get_gh_username() == "octocat"


def test_get_gh_username_without_a_login_raises(mocker) -> None:  # noqa: ANN001
    mocker.patch.object(stack_git, "get_command_output", return_value="{}")
    with pytest.raises(GitError):
        get_gh_username()


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
