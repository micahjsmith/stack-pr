"""`submit --stash` must only restore the stash it created itself."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.append(str(Path(__file__).parent.parent / "src"))

from stack_pr import cli


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],  # noqa: S607
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


@pytest.fixture
def repo(tmp_path, monkeypatch, mocker) -> Path:  # noqa: ANN001
    """A real git repo with one commit, with GitHub-facing work mocked out."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("STACKPR_CONFIG", str(tmp_path / "missing.cfg"))
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "user.email", "test@example.com")
    (repo / "file.txt").write_text("original\n")
    _git(repo, "add", "file.txt")
    _git(repo, "commit", "-q", "-m", "initial")
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
    _git(repo, "stash", "push", "-q", "-m", "unrelated")
    stash_before = _git(repo, "stash", "list")

    _run_submit(monkeypatch, *flags)

    assert _git(repo, "stash", "list") == stash_before
    assert (repo / "file.txt").read_text() == "original\n"


@pytest.mark.parametrize("flags", [(), ("--verbose",)])
def test_stash_stashes_and_restores_local_changes(
    repo: Path, monkeypatch: pytest.MonkeyPatch, flags: tuple[str, ...]
) -> None:
    (repo / "file.txt").write_text("unrelated work\n")
    _git(repo, "stash", "push", "-q", "-m", "unrelated")
    stash_before = _git(repo, "stash", "list")
    (repo / "file.txt").write_text("local change\n")

    seen_during_submit: list[str] = []
    cli.command_submit.side_effect = lambda *_a, **_k: seen_during_submit.append(
        (repo / "file.txt").read_text()
    )

    _run_submit(monkeypatch, *flags)

    # The submit ran against a clean tree, and the changes came back afterwards.
    assert seen_during_submit == ["original\n"]
    assert (repo / "file.txt").read_text() == "local change\n"
    assert _git(repo, "stash", "list") == stash_before
