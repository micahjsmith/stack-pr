import subprocess
from pathlib import Path

import pytest

from stack_pr import cli
from stack_pr.cli import (
    RE_STACK_INFO_LINE,
    format_stack_info,
    get_adopt_pr_info,
    select_adopt_entry,
)
from stack_pr.errors import StackPRError
from tests.helpers import common_args, git, init_repo, mock_entry


def test_format_stack_info() -> None:
    assert (
        format_stack_info("https://github.com/o/r/pull/3", "feat")
        == "stack-info: PR: https://github.com/o/r/pull/3, branch: feat"
    )


def test_format_stack_info_roundtrips_with_regex() -> None:
    """A formatted trailer must be parseable by the metadata regex."""
    pr = "https://github.com/o/r/pull/42"
    branch = "user/feature"
    msg = "Some title\n\nSome body\n\n" + format_stack_info(pr, branch)

    m = RE_STACK_INFO_LINE.search(msg)
    assert m is not None
    assert m.group(1) == pr
    assert m.group(2) == branch


def test_get_adopt_pr_info_no_arg_uses_current_branch(mocker) -> None:  # noqa: ANN001
    out = '{"number": 7, "headRefName": "feat", "state": "OPEN", "url": "u"}'
    spy = mocker.patch(
        "stack_pr.shell_commands.run_with_retry",
        return_value=subprocess.CompletedProcess([], 0, stdout=out, stderr=""),
    )

    info = get_adopt_pr_info(None)

    assert info["number"] == 7
    cmd = spy.call_args.args[0]
    # No PR specified -> 'gh pr view' resolves the current branch's PR.
    assert cmd[:3] == ["gh", "pr", "view"]
    assert "--json" in cmd


def test_get_adopt_pr_info_with_arg(mocker) -> None:  # noqa: ANN001
    out = '{"number": 9, "headRefName": "feat", "state": "OPEN", "url": "u"}'
    spy = mocker.patch(
        "stack_pr.shell_commands.run_with_retry",
        return_value=subprocess.CompletedProcess([], 0, stdout=out, stderr=""),
    )

    get_adopt_pr_info("9")

    cmd = spy.call_args.args[0]
    assert "9" in cmd


def test_select_adopt_entry_defaults_to_bottom() -> None:
    st = [
        mock_entry(commit_msg="bottom", commit_id="aaa"),
        mock_entry(commit_msg="top", commit_id="bbb"),
    ]
    assert select_adopt_entry(st, None) is st[0]


def test_select_adopt_entry_matches_commit(mocker) -> None:  # noqa: ANN001
    st = [
        mock_entry(commit_msg="bottom", commit_id="aaa"),
        mock_entry(commit_msg="top", commit_id="bbb"),
    ]
    mocker.patch("stack_pr.cli.get_command_output", return_value="bbb")
    assert select_adopt_entry(st, "HEAD") is st[1]


def test_select_adopt_entry_commit_not_in_stack(mocker) -> None:  # noqa: ANN001
    st = [mock_entry(commit_msg="bottom", commit_id="aaa")]
    mocker.patch("stack_pr.cli.get_command_output", return_value="zzz")
    with pytest.raises(StackPRError):
        select_adopt_entry(st, "deadbeef")


def test_command_adopt_refuses_already_managed(mocker) -> None:  # noqa: ANN001
    msg = "Title\n\nstack-info: PR: https://x/pull/1, branch: feat\n"
    mocker.patch("stack_pr.cli.get_stack", return_value=[mock_entry(commit_msg=msg)])

    with pytest.raises(StackPRError):
        cli.command_adopt(common_args(), None, None)


def test_command_adopt_refuses_non_open_pr(mocker) -> None:  # noqa: ANN001
    mocker.patch(
        "stack_pr.cli.get_stack",
        return_value=[mock_entry(commit_msg="Plain title\n\nbody")],
    )
    mocker.patch(
        "stack_pr.cli.get_adopt_pr_info",
        return_value={"state": "MERGED", "url": "u", "headRefName": "feat"},
    )

    with pytest.raises(StackPRError):
        cli.command_adopt(common_args(), "5", None)


@pytest.mark.usefixtures("gh_username")
def test_command_adopt_embeds_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mocker,  # noqa: ANN001
) -> None:
    origin = tmp_path / "origin.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    repo = init_repo(tmp_path / "repo")
    git(repo, "remote", "add", "origin", str(origin))
    git(repo, "push", "-q", "origin", "main")
    git(repo, "checkout", "-q", "-b", "feat")
    (repo / "file.txt").write_text("feature\n")
    git(repo, "commit", "-q", "-am", "Plain title\n\nbody")
    monkeypatch.chdir(repo)
    url = "https://github.com/o/r/pull/5"
    mocker.patch(
        "stack_pr.cli.get_adopt_pr_info",
        return_value={"state": "OPEN", "url": url, "headRefName": "feat"},
    )

    cli.command_adopt(common_args(), "5", None)

    # The commit now carries the PR's metadata, pointing at the PR's head ref.
    msg = git(repo, "log", "-1", "--format=%B").strip()
    assert msg == "Plain title\n\nbody\n\n" + format_stack_info(url, "feat")
    assert git(repo, "symbolic-ref", "--short", "HEAD").strip() == "feat"
