"""`submit --stash` must only restore the stash it created itself."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from stack_pr import cli
from tests.helpers import git, init_repo


@pytest.fixture
def repo(tmp_path, monkeypatch, mocker) -> Path:  # noqa: ANN001
    """A real git repo with one commit, with GitHub-facing work mocked out."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("STACKPR_CONFIG", str(tmp_path / "missing.cfg"))
    repo = init_repo(tmp_path / "repo", content="original\n")
    monkeypatch.chdir(repo)

    mocker.patch.object(cli, "check_gh_installed")
    mocker.patch.object(cli, "get_gh_username", return_value="someone")
    mocker.patch.object(cli, "check_target_branch_exists")
    mocker.patch.object(cli, "deduce_base", side_effect=lambda args: args)
    mocker.patch.object(cli, "command_submit")
    return repo


def _run_submit(monkeypatch: pytest.MonkeyPatch, *flags: str) -> None:
    monkeypatch.setattr(sys, "argv", ["stack-pr", "submit", "--stash", *flags])
    cli.main()


@pytest.mark.parametrize("flags", [(), ("--verbose",)])
def test_stash_leaves_unrelated_stash_alone_when_tree_is_clean(
    repo: Path, monkeypatch: pytest.MonkeyPatch, flags: tuple[str, ...]
) -> None:
    (repo / "file.txt").write_text("unrelated work\n")
    git(repo, "stash", "push", "-q", "-m", "unrelated")
    stash_before = git(repo, "stash", "list")

    _run_submit(monkeypatch, *flags)

    assert git(repo, "stash", "list") == stash_before
    assert (repo / "file.txt").read_text() == "original\n"


@pytest.mark.parametrize("flags", [(), ("--verbose",)])
def test_stash_stashes_and_restores_local_changes(
    repo: Path, monkeypatch: pytest.MonkeyPatch, flags: tuple[str, ...]
) -> None:
    (repo / "file.txt").write_text("unrelated work\n")
    git(repo, "stash", "push", "-q", "-m", "unrelated")
    stash_before = git(repo, "stash", "list")
    (repo / "file.txt").write_text("local change\n")

    seen_during_submit: list[str] = []
    cli.command_submit.side_effect = lambda *_a, **_k: seen_during_submit.append(
        (repo / "file.txt").read_text()
    )

    _run_submit(monkeypatch, *flags)

    # The submit ran against a clean tree, and the changes came back afterwards.
    assert seen_during_submit == ["original\n"]
    assert (repo / "file.txt").read_text() == "local change\n"
    assert git(repo, "stash", "list") == stash_before
