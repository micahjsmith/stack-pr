import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

sys.path.append(str(Path(__file__).parent.parent / "src"))

from stack_pr import cli
from stack_pr.cli import CommitHeader, StackEntry, delete_remote_branches

TEMPLATE = "$USERNAME/stack/$ID"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],  # noqa: S607
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def _remote_branches(remote: Path) -> set[str]:
    out = _git(remote, "for-each-ref", "refs/heads", "--format=%(refname:short)")
    return set(out.split())


@pytest.fixture
def repo(tmp_path: Path, monkeypatch, mocker) -> Iterator[tuple[Path, Path]]:  # noqa: ANN001
    """A local clone with a bare 'origin' holding two stack branches and main."""
    remote = tmp_path / "remote.git"
    local = tmp_path / "local"
    _git(tmp_path, "init", "--bare", "-b", "main", str(remote))
    _git(tmp_path, "init", "-b", "main", str(local))
    _git(local, "config", "user.email", "test@example.com")
    _git(local, "config", "user.name", "Test")
    _git(local, "commit", "--allow-empty", "-m", "init")
    _git(local, "remote", "add", "origin", str(remote))
    for branch in ("main", "alice/stack/1", "alice/stack/2", "alice/other"):
        _git(local, "push", "origin", f"HEAD:refs/heads/{branch}")

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
