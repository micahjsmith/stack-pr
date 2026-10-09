"""`stack-pr land`: merge the bottom PR, rebase the rest onto the target."""

from __future__ import annotations

from pathlib import Path

import pytest

from stack_pr.cli import command_land
from tests.helpers import (
    FakeGitHub,
    branches,
    commit_message,
    common_args,
    git,
    init_stack_repo,
    stack_info,
)

pytestmark = pytest.mark.usefixtures("gh_username")


def _rev(repo: Path, rev: str) -> str:
    return git(repo, "rev-parse", rev).strip()


@pytest.fixture
def stack(tmp_path: Path, monkeypatch, fake_gh: FakeGitHub) -> tuple[Path, Path]:  # noqa: ANN001
    """A submitted two-PR stack: #1 into main, #2 into #1's branch."""
    local, remote = init_stack_repo(tmp_path, 2, submitted=True)
    fake_gh.remote = remote
    fake_gh.add_pr(1, head="TestBot/stack/1", base="main")
    fake_gh.add_pr(2, head="TestBot/stack/2", base="TestBot/stack/1")
    monkeypatch.chdir(local)
    return local, remote


def test_land_squash_merges_the_bottom_pr(stack, fake_gh) -> None:  # noqa: ANN001
    _local, remote = stack

    command_land(common_args())

    merge = next(cmd for cmd in fake_gh.mutations() if cmd[2] == "merge")
    assert merge[:4] == ["gh", "pr", "merge", "https://github.com/o/r/pull/1"]
    # GitHub's squash commit gets the commit's message, minus the metadata,
    # with the PR number appended to the title.
    landed = commit_message(remote, "main")
    assert landed.splitlines()[0] == "c1 (#1)"
    assert "Body of c1." in landed
    assert "stack-info" not in landed


def test_land_rebases_the_rest_of_the_stack_onto_the_target(stack, fake_gh) -> None:  # noqa: ANN001
    _local, remote = stack

    command_land(common_args())

    # The bottom PR is retargeted at main before the merge, and the new bottom
    # PR afterwards, once its branch no longer contains the landed commit.
    edits = [cmd for cmd in fake_gh.mutations() if cmd[2] == "edit"]
    assert edits == [
        ["gh", "pr", "edit", "https://github.com/o/r/pull/1", "-B", "main"],
        ["gh", "pr", "edit", "https://github.com/o/r/pull/2", "-B", "main"],
    ]
    # PR #2's branch was rebased onto the landed commit and force-pushed, with
    # its metadata intact.
    assert _rev(remote, "TestBot/stack/2^") == _rev(remote, "main")
    assert commit_message(remote, "TestBot/stack/2").endswith(
        stack_info(2, "TestBot/stack/2")
    )


def test_land_leaves_the_user_on_their_branch_rebased_onto_the_target(stack) -> None:  # noqa: ANN001
    local, remote = stack

    command_land(common_args())

    assert git(local, "branch", "--show-current").strip() == "feature"
    # The landed commit dropped out of the branch; only the rest of the stack
    # remains on top of the updated target.
    assert _rev(local, "feature^") == _rev(remote, "main")
    assert git(local, "log", "-1", "--format=%s", "feature").strip() == "c2"
    # The local target is fast-forwarded, and the temporary branches are gone.
    assert _rev(local, "main") == _rev(remote, "main")
    assert branches(local) == {"main", "feature"}


def test_land_a_single_pr_stack(tmp_path: Path, monkeypatch, fake_gh) -> None:  # noqa: ANN001
    local, remote = init_stack_repo(tmp_path, 1, submitted=True)
    fake_gh.remote = remote
    fake_gh.add_pr(1, head="TestBot/stack/1")
    monkeypatch.chdir(local)

    command_land(common_args())

    assert [cmd[2] for cmd in fake_gh.mutations()] == ["edit", "merge"]
    assert commit_message(remote, "main").startswith("c1 (#1)")


@pytest.mark.xfail(
    strict=True,
    reason="land never fetches after merging the last PR, so with a one-PR stack "
    "the user's branch is rebased onto a stale origin/main",
)
def test_land_a_single_pr_stack_updates_the_users_branch(
    tmp_path: Path,
    monkeypatch,  # noqa: ANN001
    fake_gh,  # noqa: ANN001
) -> None:
    local, remote = init_stack_repo(tmp_path, 1, submitted=True)
    fake_gh.remote = remote
    fake_gh.add_pr(1, head="TestBot/stack/1")
    monkeypatch.chdir(local)

    command_land(common_args())

    assert _rev(local, "feature") == _rev(remote, "main")


def test_land_an_empty_stack_does_nothing(tmp_path: Path, monkeypatch, fake_gh) -> None:  # noqa: ANN001
    local, _remote = init_stack_repo(tmp_path, 0, submitted=False)
    monkeypatch.chdir(local)

    command_land(common_args())

    assert fake_gh.commands == []


@pytest.mark.parametrize(
    ("pr_fields", "reason"),
    [
        ({"state": "MERGED"}, "not in 'OPEN' state"),
        ({"mergeStateStatus": "BLOCKED"}, "not mergeable"),
        ({"baseRefName": "develop"}, "Base branch name on github mismatches"),
    ],
)
def test_land_refuses_a_stack_that_fails_verification(
    stack,  # noqa: ANN001
    fake_gh,  # noqa: ANN001
    capsys,  # noqa: ANN001
    pr_fields: dict[str, str],
    reason: str,
) -> None:
    _local, remote = stack
    main_before = _rev(remote, "main")
    fake_gh.prs[1].update(pr_fields)

    with pytest.raises(RuntimeError):
        command_land(common_args())

    assert reason in capsys.readouterr().out
    assert fake_gh.mutations() == []
    assert _rev(remote, "main") == main_before


def test_land_refuses_commits_that_were_never_submitted(
    tmp_path: Path,
    monkeypatch,  # noqa: ANN001
    fake_gh,  # noqa: ANN001
    capsys,  # noqa: ANN001
) -> None:
    local, _remote = init_stack_repo(tmp_path, 1, submitted=False)
    monkeypatch.chdir(local)

    with pytest.raises(RuntimeError):
        command_land(common_args())

    assert "missing some information" in capsys.readouterr().out
    assert fake_gh.commands == []
