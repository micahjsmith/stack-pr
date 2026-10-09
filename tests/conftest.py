from __future__ import annotations

from collections.abc import Iterator
from unittest.mock import Mock

import pytest

from stack_pr import cli
from stack_pr.git import git_config
from tests.helpers import FakeShell


@pytest.fixture
def autoland_console(mocker) -> Mock:  # noqa: ANN001
    """Silence autoland's console; the mock scripts input and records output."""
    return mocker.patch("stack_pr.autoland.console")


@pytest.fixture
def gh_username() -> Iterator[str]:
    """Pin the GitHub username to "TestBot" without asking gh."""
    previous = git_config.username_override
    git_config.set_username_override("TestBot")
    cli.get_branch_name_base.cache_clear()
    yield "TestBot"
    git_config.set_username_override(previous)
    cli.get_branch_name_base.cache_clear()


@pytest.fixture
def fake_shell(mocker) -> FakeShell:  # noqa: ANN001
    """Replace cli.run_shell_command with a FakeShell."""
    fake = FakeShell()
    mocker.patch("stack_pr.cli.run_shell_command", side_effect=fake)
    return fake
