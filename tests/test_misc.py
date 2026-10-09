import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from stack_pr import cli
from stack_pr.cli import (
    generate_available_branch_name,
    generate_branch_name,
    get_branch_id,
    get_taken_branch_ids,
    load_config,
)
from stack_pr.git import git_config, is_rebase_in_progress
from stack_pr.shell_commands import run_shell_command


@pytest.mark.parametrize(
    ("template", "branch_name", "expected"),
    [
        ("feature-$ID-desc", "feature-123-desc", "123"),
        ("$USERNAME/stack/$ID", "{username}/stack/99", "99"),
        ("$USERNAME/stack/$ID", "refs/remotes/origin/{username}/stack/99", "99"),
        ("feat+x/$ID", "feat+x/7", "7"),
    ],
)
def test_get_branch_id(
    gh_username: str, template: str, branch_name: str, expected: str
) -> None:
    branch_name = branch_name.format(username=gh_username)
    assert get_branch_id(template, branch_name) == expected


@pytest.mark.parametrize(
    ("template", "branch_name"),
    [
        ("feature/$ID/desc", "feature/abc/desc"),
        ("feature/$ID/desc", "wrong/format"),
        ("$USERNAME/stack/$ID", "{username}/main/99"),
        # A branch belonging to a user whose name merely ends with ours.
        ("$USERNAME/stack/$ID", "Not{username}/stack/3"),
        ("$USERNAME/stack/$ID", "refs/remotes/origin/Not{username}/stack/3"),
        # A branch that merely starts with one of ours.
        ("$USERNAME/stack/$ID", "{username}/stack/3-old"),
        ("$USERNAME/stack/$ID", "archive/{username}/stack/3"),
        # Literal parts of the template are not regex syntax.
        ("feat+x/$ID", "feattx/7"),
        ("feat+x/$ID", "featx/7"),
    ],
)
def test_get_branch_id_no_match(
    gh_username: str, template: str, branch_name: str
) -> None:
    branch_name = branch_name.format(username=gh_username)
    assert get_branch_id(template, branch_name) is None


@pytest.fixture
def dotted_username() -> Iterator[str]:
    previous = git_config.username_override
    git_config.set_username_override("john.doe")
    cli.get_branch_name_base.cache_clear()
    yield "john.doe"
    git_config.set_username_override(previous)
    cli.get_branch_name_base.cache_clear()


@pytest.mark.usefixtures("dotted_username")
def test_get_branch_id_with_dotted_username() -> None:
    template = "$USERNAME/stack/$ID"
    assert get_branch_id(template, "john.doe/stack/5") == "5"
    assert get_branch_id(template, "refs/remotes/origin/john.doe/stack/5") == "5"
    assert get_branch_id(template, "johnxdoe/stack/5") is None


@pytest.mark.usefixtures("gh_username")
def test_generate_branch_name() -> None:
    template = "feature/$ID/description"
    assert generate_branch_name(template, 123) == "feature/123/description"


@pytest.mark.usefixtures("gh_username")
def test_get_taken_branch_ids() -> None:
    template = "$USERNAME/stack/$ID"
    refs = [
        "refs/remotes/origin/TestBot/stack/104",
        "refs/remotes/origin/TestBot/stack/105",
        "refs/remotes/origin/TestBot/stack/134",
    ]
    assert get_taken_branch_ids(refs, template) == [104, 105, 134]
    refs = ["TestBot/stack/104", "TestBot/stack/105", "TestBot/stack/134"]
    assert get_taken_branch_ids(refs, template) == [104, 105, 134]
    refs = [
        "TestBot/stack/104",
        "AAAA/stack/105",
        "TestBot/stack/134",
        "TestBot/stack/bbb",
    ]
    assert get_taken_branch_ids(refs, template) == [104, 134]
    refs = [
        "refs/remotes/origin/TestBot/stack/104",
        "refs/remotes/origin/NotTestBot/stack/900",
        "refs/remotes/origin/TestBot/stack/901-old",
        "refs/remotes/origin/archive/TestBot/stack/902",
    ]
    assert get_taken_branch_ids(refs, template) == [104]


@pytest.mark.usefixtures("gh_username")
def test_generate_available_branch_name() -> None:
    template = "$USERNAME/stack/$ID"
    refs = [
        "refs/remotes/origin/TestBot/stack/104",
        "refs/remotes/origin/TestBot/stack/105",
        "refs/remotes/origin/TestBot/stack/134",
    ]
    assert generate_available_branch_name(refs, template) == "TestBot/stack/135"
    refs = []
    assert generate_available_branch_name(refs, template) == "TestBot/stack/1"
    template = "$USERNAME-stack-$ID"
    refs = [
        "refs/remotes/origin/TestBot-stack-104",
        "refs/remotes/origin/TestBot-stack-105",
        "refs/remotes/origin/TestBot-stack-134",
    ]
    assert generate_available_branch_name(refs, template) == "TestBot-stack-135"


def test_is_rebase_in_progress() -> None:
    """Test the is_rebase_in_progress function with different git states."""
    with tempfile.TemporaryDirectory() as temp_dir:
        repo_dir = Path(temp_dir)
        run_shell_command(["git", "init", "-q"], cwd=repo_dir, quiet=True)
        git_dir = repo_dir / ".git"

        # Test no rebase in progress
        assert not is_rebase_in_progress(repo_dir)

        # Test rebase-merge directory exists
        rebase_merge_dir = git_dir / "rebase-merge"
        rebase_merge_dir.mkdir()
        assert is_rebase_in_progress(repo_dir)

        # Clean up and test rebase-apply directory
        rebase_merge_dir.rmdir()
        assert not is_rebase_in_progress(repo_dir)

        rebase_apply_dir = git_dir / "rebase-apply"
        rebase_apply_dir.mkdir()
        assert is_rebase_in_progress(repo_dir)

        # Test both directories exist
        rebase_merge_dir.mkdir()
        assert is_rebase_in_progress(repo_dir)

        # Test with None repo_dir (current directory)
        # This should not raise an error even if .git doesn't exist in cwd
        assert not is_rebase_in_progress(None)


def test_load_config_reads_the_file(tmp_path: Path) -> None:
    cfg_file = tmp_path / ".stack-pr.cfg"
    cfg_file.write_text("[repo]\ntarget = develop\n")

    assert load_config(cfg_file).get("repo", "target") == "develop"


def test_load_config_without_a_file_is_empty(tmp_path: Path) -> None:
    assert load_config(tmp_path / "missing.cfg").sections() == []
