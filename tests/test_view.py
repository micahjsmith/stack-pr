"""`stack-pr view`: print the stack without changing anything."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from stack_pr.cli import command_view
from tests.helpers import branches, common_args, git, init_stack_repo

pytestmark = pytest.mark.usefixtures("gh_username")

RE_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _view(capsys, **overrides: object) -> list[str]:  # noqa: ANN001
    command_view(common_args(**overrides))
    return RE_ANSI.sub("", capsys.readouterr().out).splitlines()


def _short(repo: Path, rev: str) -> str:
    return git(repo, "rev-parse", "--short=8", rev).strip()


@pytest.fixture
def partly_submitted(tmp_path: Path, monkeypatch) -> Path:  # noqa: ANN001
    """'feature' holds c1, submitted as PR #1, and c2, not submitted yet."""
    local, _remote = init_stack_repo(tmp_path, 1, submitted=True)
    (local / "file2.txt").write_text("2\n")
    git(local, "add", "file2.txt")
    git(local, "commit", "-q", "-m", "c2")
    monkeypatch.chdir(local)
    return local


def test_view_lists_the_stack_top_first(partly_submitted: Path, capsys) -> None:  # noqa: ANN001
    local = partly_submitted

    out = _view(capsys)

    entries = [line.strip() for line in out if line.strip().startswith("* ")]
    assert entries == [
        f"* {_short(local, 'HEAD')} (no PR, 'TestBot/stack/2' -> 'TestBot/stack/1'): c2",
        f"* {_short(local, 'HEAD~1')} (#1, 'TestBot/stack/1' -> 'main'): c1",
    ]


def test_view_changes_no_branches(partly_submitted: Path, capsys) -> None:  # noqa: ANN001
    local = partly_submitted
    head_before = git(local, "rev-parse", "HEAD")

    _view(capsys)

    assert branches(local) == {"main", "feature"}
    assert git(local, "rev-parse", "HEAD") == head_before


def test_view_tips_say_an_unsubmitted_stack_needs_exporting(
    partly_submitted: Path,
    capsys,  # noqa: ANN001
) -> None:
    out = "\n".join(_view(capsys, show_tips=True))

    assert "can't be landed yet" in out


def test_view_tips_say_a_submitted_stack_is_ready_to_land(
    tmp_path: Path,
    monkeypatch,  # noqa: ANN001
    capsys,  # noqa: ANN001
) -> None:
    local, _remote = init_stack_repo(tmp_path, 2, submitted=True)
    monkeypatch.chdir(local)

    out = "\n".join(_view(capsys, show_tips=True))

    assert "This stack is ready to land!" in out
    assert "stack-pr land" in out


def test_view_warns_when_the_local_base_is_behind_the_remote(
    tmp_path: Path,
    monkeypatch,  # noqa: ANN001
    capsys,  # noqa: ANN001
) -> None:
    local, _remote = init_stack_repo(tmp_path, 1, submitted=False)
    # Someone else lands a commit on main; 'feature' is rebased onto it, but
    # the local 'main' is left behind.
    git(local, "checkout", "-q", "-b", "upstream", "main")
    (local / "upstream.txt").write_text("u\n")
    git(local, "add", "upstream.txt")
    git(local, "commit", "-q", "-m", "upstream")
    git(local, "push", "-q", "origin", "upstream:refs/heads/main")
    git(local, "checkout", "-q", "feature")
    git(local, "branch", "-q", "-D", "upstream")
    git(local, "rebase", "-q", "origin/main")
    monkeypatch.chdir(local)

    out = "\n".join(_view(capsys))

    assert "Warning: Local 'main' is behind 'origin/main'!" in out
    assert "git rebase origin/main main" in out
