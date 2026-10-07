"""Error conditions must make stack-pr exit non-zero.

Scripts (and autoland, which shells out to stack-pr) rely on the exit code to
tell whether a command succeeded.
"""

import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from unittest import mock

import pytest

sys.path.append(str(Path(__file__).parent.parent / "src"))

from stack_pr import cli


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)  # noqa: S607


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.name", "Test")
    _git(tmp_path, "config", "user.email", "test@example.com")
    (tmp_path / "file.txt").write_text("one\n")
    _git(tmp_path, "add", "file.txt")
    _git(tmp_path, "commit", "-q", "-m", "initial")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("STACKPR_CONFIG", raising=False)
    yield tmp_path
    # main() caches the branch name base; don't leak it to other tests.
    cli.get_branch_name_base.cache_clear()


def test_submit_with_uncommitted_changes_exits_nonzero(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (repo / "file.txt").write_text("modified\n")
    monkeypatch.setattr(sys, "argv", ["stack-pr", "submit"])

    with (
        mock.patch.object(cli, "check_gh_installed"),
        mock.patch.object(cli, "get_gh_username", return_value="testuser"),
        pytest.raises(SystemExit) as excinfo,
    ):
        cli.main()

    assert excinfo.value.code not in (0, None)


def test_submit_draft_bitmask_mismatch_exits_nonzero(repo: Path) -> None:
    args = cli.CommonArgs(
        base="main",
        head="HEAD",
        remote="origin",
        target="main",
        hyperlinks=False,
        verbose=False,
        branch_name_template="$USERNAME/stack",
        show_tips=False,
        land_disabled=False,
    )
    two_entries = [mock.MagicMock(), mock.MagicMock()]

    with (
        mock.patch.object(cli, "should_update_local_base", return_value=False),
        mock.patch.object(cli, "get_stack", return_value=two_entries),
        pytest.raises(SystemExit) as excinfo,
    ):
        cli.command_submit(
            args, draft=False, reviewer="", draft_bitmask=[True, False, True]
        )

    assert excinfo.value.code not in (0, None)
