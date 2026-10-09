from collections.abc import Iterator
from pathlib import Path

import pytest

from stack_pr import cli
from stack_pr.cli import CommitHeader, StackEntry, delete_remote_branches
from tests.helpers import git, init_repo

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
