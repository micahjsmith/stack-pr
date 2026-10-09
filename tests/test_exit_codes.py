"""Error conditions must make stack-pr exit non-zero.

Scripts (and autoland, which shells out to stack-pr) rely on the exit code to
tell whether a command succeeded.
"""

import sys
from collections.abc import Iterator
from pathlib import Path
from unittest import mock

import pytest

from stack_pr import cli
from stack_pr.errors import StackPRError
from tests.helpers import common_args, init_repo


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    init_repo(tmp_path, content="one\n")
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


def test_submit_draft_bitmask_mismatch_is_an_error(repo: Path) -> None:
    args = common_args(branch_name_template="$USERNAME/stack")
    two_entries = [mock.MagicMock(), mock.MagicMock()]

    with (
        mock.patch.object(cli, "should_update_local_base", return_value=False),
        mock.patch.object(cli, "get_stack", return_value=two_entries),
        pytest.raises(StackPRError, match="Draft bitmask"),
    ):
        cli.command_submit(
            args, draft=False, reviewer="", draft_bitmask=[True, False, True]
        )


def test_no_subcommand_is_a_usage_error(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "argv", ["stack-pr"])

    with pytest.raises(SystemExit) as excinfo:
        cli.main()

    # 2 matches argparse's exit status for usage errors.
    assert excinfo.value.code == 2
    assert "usage:" in capsys.readouterr().out


def test_help_subcommand_exits_zero(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "argv", ["stack-pr", "help"])

    cli.main()  # returning normally means exit status 0

    assert "usage:" in capsys.readouterr().out
