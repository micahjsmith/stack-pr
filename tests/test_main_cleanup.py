"""A failed cleanup in main() must not hide the error that triggered it."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from stack_pr import cli
from stack_pr.errors import StackPRError
from tests.helpers import git, init_repo


@pytest.fixture
def repo(tmp_path, monkeypatch, mocker) -> Path:  # noqa: ANN001
    """A real git repo on 'main', with GitHub-facing work mocked out."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("STACKPR_CONFIG", str(tmp_path / "missing.cfg"))
    repo = init_repo(tmp_path / "repo")
    monkeypatch.chdir(repo)

    mocker.patch.object(cli, "check_gh_installed")
    mocker.patch.object(cli, "get_gh_username", return_value="someone")
    mocker.patch.object(cli, "check_target_branch_exists")
    mocker.patch.object(cli, "deduce_base", side_effect=lambda args: args)
    return repo


def _fail_mid_rebase(repo: Path) -> None:
    """Stop a rebase on a conflict, so checking out 'main' fails, then fail."""
    git(repo, "checkout", "-q", "-b", "feature")
    (repo / "file.txt").write_text("feature\n")
    git(repo, "commit", "-q", "-am", "feature")
    git(repo, "checkout", "-q", "-b", "other", "main")
    (repo / "file.txt").write_text("other\n")
    git(repo, "commit", "-q", "-am", "other")
    git(repo, "rebase", "feature", check=False)
    msg = "the original land failure"
    raise RuntimeError(msg)


def test_failed_checkout_back_does_not_mask_original_error(
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    mocker,  # noqa: ANN001
    capsys: pytest.CaptureFixture[str],
) -> None:
    mocker.patch.object(
        cli, "command_land", side_effect=lambda _: _fail_mid_rebase(repo)
    )
    monkeypatch.setattr(sys, "argv", ["stack-pr", "land"])

    with pytest.raises(RuntimeError, match="the original land failure"):
        cli.main()

    # The checkout back really did fail, and the user is told where they were.
    assert git(repo, "branch", "--show-current").strip() != "main"
    out = capsys.readouterr().out
    assert "main" in out
    assert "git checkout main" in out


def test_user_error_is_reported_after_returning_to_original_branch(
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    mocker,  # noqa: ANN001
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail_on_another_branch(_args: object) -> None:
        git(repo, "checkout", "-q", "-b", "elsewhere")
        msg = "the stack can't be landed"
        raise StackPRError(msg)

    mocker.patch.object(cli, "command_land", side_effect=fail_on_another_branch)
    monkeypatch.setattr(sys, "argv", ["stack-pr", "land"])

    with pytest.raises(SystemExit) as excinfo:
        cli.main()

    assert excinfo.value.code == 1
    out = capsys.readouterr().out
    assert "ERROR: " in out
    assert "the stack can't be landed" in out
    assert git(repo, "branch", "--show-current").strip() == "main"
