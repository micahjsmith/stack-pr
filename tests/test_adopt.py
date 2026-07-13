import sys
from pathlib import Path

sys.path.append(str(Path(__file__).parent.parent / "src"))

import pytest

from stack_pr import cli
from stack_pr.cli import (
    RE_STACK_INFO_LINE,
    format_stack_info,
    get_adopt_pr_info,
    select_adopt_entry,
)


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
    spy = mocker.patch("stack_pr.cli.get_command_output", return_value=out)

    info = get_adopt_pr_info(None)

    assert info["number"] == 7
    cmd = spy.call_args.args[0]
    # No PR specified -> 'gh pr view' resolves the current branch's PR.
    assert cmd[:3] == ["gh", "pr", "view"]
    assert "--json" in cmd


def test_get_adopt_pr_info_with_arg(mocker) -> None:  # noqa: ANN001
    out = '{"number": 9, "headRefName": "feat", "state": "OPEN", "url": "u"}'
    spy = mocker.patch("stack_pr.cli.get_command_output", return_value=out)

    get_adopt_pr_info("9")

    cmd = spy.call_args.args[0]
    assert "9" in cmd


def _fake_entry(mocker, *, commit_msg: str, commit_id: str = "abc123"):  # noqa: ANN001, ANN202
    commit = mocker.Mock()
    commit.commit_msg.return_value = commit_msg
    commit.commit_id.return_value = commit_id
    commit.tree.return_value = "tree-sha"
    entry = mocker.Mock()
    entry.commit = commit
    entry.pprint.return_value = "entry"
    return entry


def _fake_stack(mocker, *, commit_msg: str):  # noqa: ANN001, ANN202
    return [_fake_entry(mocker, commit_msg=commit_msg)]


def _common_args() -> cli.CommonArgs:
    return cli.CommonArgs(
        base="main",
        head="HEAD",
        remote="origin",
        target="main",
        hyperlinks=False,
        verbose=False,
        branch_name_template="$USERNAME/stack/$ID",
        show_tips=False,
        land_disabled=False,
    )


def test_select_adopt_entry_defaults_to_bottom(mocker) -> None:  # noqa: ANN001
    st = [
        _fake_entry(mocker, commit_msg="bottom", commit_id="aaa"),
        _fake_entry(mocker, commit_msg="top", commit_id="bbb"),
    ]
    assert select_adopt_entry(st, None) is st[0]


def test_select_adopt_entry_matches_commit(mocker) -> None:  # noqa: ANN001
    st = [
        _fake_entry(mocker, commit_msg="bottom", commit_id="aaa"),
        _fake_entry(mocker, commit_msg="top", commit_id="bbb"),
    ]
    mocker.patch("stack_pr.cli.get_command_output", return_value="bbb")
    assert select_adopt_entry(st, "HEAD") is st[1]


def test_select_adopt_entry_commit_not_in_stack(mocker) -> None:  # noqa: ANN001
    st = [_fake_entry(mocker, commit_msg="bottom", commit_id="aaa")]
    mocker.patch("stack_pr.cli.get_command_output", return_value="zzz")
    with pytest.raises(SystemExit):
        select_adopt_entry(st, "deadbeef")


def test_command_adopt_refuses_already_managed(mocker) -> None:  # noqa: ANN001
    msg = "Title\n\nstack-info: PR: https://x/pull/1, branch: feat\n"
    mocker.patch(
        "stack_pr.cli.get_stack", return_value=_fake_stack(mocker, commit_msg=msg)
    )

    with pytest.raises(SystemExit):
        cli.command_adopt(_common_args(), None, None)


def test_command_adopt_refuses_non_open_pr(mocker) -> None:  # noqa: ANN001
    mocker.patch(
        "stack_pr.cli.get_stack",
        return_value=_fake_stack(mocker, commit_msg="Plain title\n\nbody"),
    )
    mocker.patch(
        "stack_pr.cli.get_adopt_pr_info",
        return_value={"state": "MERGED", "url": "u", "headRefName": "feat"},
    )

    with pytest.raises(SystemExit):
        cli.command_adopt(_common_args(), "5", None)


def test_command_adopt_embeds_metadata(mocker) -> None:  # noqa: ANN001
    stack = _fake_stack(mocker, commit_msg="Plain title\n\nbody")
    # First call returns the unmanaged stack; second call (after adoption) is
    # only used to print, so the same stack is fine.
    mocker.patch("stack_pr.cli.get_stack", return_value=stack)
    mocker.patch(
        "stack_pr.cli.get_adopt_pr_info",
        return_value={
            "state": "OPEN",
            "url": "https://github.com/o/r/pull/5",
            "headRefName": "feat",
            "headRefOid": "deadbeef",
        },
    )
    mocker.patch("stack_pr.cli.get_current_branch_name", return_value="feat")
    mocker.patch("stack_pr.cli.warn_if_content_differs")
    mocker.patch("stack_pr.cli.set_head_branches")
    mocker.patch("stack_pr.cli.set_base_branches")
    mocker.patch("stack_pr.cli.print_stack")
    adopt_spy = mocker.patch("stack_pr.cli.adopt_commit")
    mocker.patch("stack_pr.cli.run_shell_command")

    cli.command_adopt(_common_args(), "5", None)

    adopt_spy.assert_called_once()
    args = adopt_spy.call_args.args
    assert args[1] == "https://github.com/o/r/pull/5"  # pr url
    assert args[2] == "feat"  # branch == PR head ref
