from collections.abc import Iterator
from pathlib import Path

import pytest

from stack_pr import cli
from stack_pr.cli import (
    CommitHeader,
    StackEntry,
    command_abandon,
    delete_remote_branches,
)
from tests.helpers import (
    branches,
    commit_message,
    common_args,
    git,
    init_repo,
    init_stack_repo,
)

TEMPLATE = "$USERNAME/stack/$ID"


def _remote_branches(remote: Path) -> set[str]:
    out = git(remote, "for-each-ref", "refs/heads", "--format=%(refname:short)")
    return set(out.split())


@pytest.fixture
def repo(tmp_path: Path, monkeypatch, mocker) -> Iterator[tuple[Path, Path]]:  # noqa: ANN001
    """A local clone with a bare 'origin' holding two stack branches and main."""
    remote = tmp_path / "remote.git"
    local = tmp_path / "local"
    git(tmp_path, "init", "--bare", "-b", "main", str(remote))
    init_repo(local)
    git(local, "remote", "add", "origin", str(remote))
    for branch in ("main", "alice/stack/1", "alice/stack/2", "alice/other"):
        git(local, "push", "origin", f"HEAD:refs/heads/{branch}")

    monkeypatch.chdir(local)
    mocker.patch.object(cli, "get_gh_username", return_value="alice")
    cli.get_branch_name_base.cache_clear()
    yield local, remote
    cli.get_branch_name_base.cache_clear()


def test_delete_remote_branches_deletes_stack_branches(repo) -> None:  # noqa: ANN001
    _local, remote = repo
    st = [
        StackEntry(CommitHeader(""), _head="alice/stack/1"),
        StackEntry(CommitHeader(""), _head="alice/stack/2"),
    ]

    delete_remote_branches(
        st, remote="origin", verbose=False, branch_name_template=TEMPLATE
    )

    assert _remote_branches(remote) == {"main", "alice/other"}


@pytest.mark.usefixtures("gh_username")
def test_abandon_strips_metadata_and_deletes_the_stack_branches(
    tmp_path: Path,
    monkeypatch,  # noqa: ANN001
) -> None:
    local, remote = init_stack_repo(tmp_path, 2, submitted=True)
    tree_before = git(local, "rev-parse", "feature^{tree}")
    monkeypatch.chdir(local)

    command_abandon(common_args())

    assert git(local, "branch", "--show-current").strip() == "feature"
    assert [commit_message(local, rev) for rev in ("feature~1", "feature")] == [
        "c1\n\nBody of c1.",
        "c2\n\nBody of c2.",
    ]
    assert git(local, "rev-parse", "feature~2") == git(local, "rev-parse", "main")
    assert git(local, "rev-parse", "feature^{tree}") == tree_before
    assert branches(local) == {"main", "feature"}
    assert _remote_branches(remote) == {"main"}


def test_abandon_an_empty_stack_changes_nothing(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    local, remote = init_stack_repo(tmp_path, 0, submitted=False)
    monkeypatch.chdir(local)
    head_before = git(local, "rev-parse", "HEAD")

    command_abandon(common_args())

    assert git(local, "rev-parse", "HEAD") == head_before
    assert _remote_branches(remote) == {"main"}
