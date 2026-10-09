from __future__ import annotations

import subprocess
from collections.abc import Iterable, Iterator
from typing import Any
from unittest.mock import Mock

import pytest

from stack_pr import cli
from stack_pr.git import git_config
from tests.helpers import FakeGitHub, FakeShell


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


@pytest.fixture
def fake_gh(mocker) -> FakeGitHub:  # noqa: ANN001
    """Route cli's `gh` commands to a FakeGitHub; everything else runs for real.

    Set the fixture's `remote` to a bare repo to have merges land there.
    """
    gh = FakeGitHub()
    real_run, real_output = cli.run_shell_command, cli.get_command_output

    def stdin(kwargs: dict[str, Any]) -> str | None:
        data = kwargs.get("input")
        return data.decode() if isinstance(data, bytes) else data

    def run(cmd: Iterable[Any], **kwargs: Any) -> subprocess.CompletedProcess:
        args = [str(c) for c in cmd]
        if args[0] != "gh":
            return real_run(args, **kwargs)
        try:
            out = gh(args, stdin(kwargs)).encode()
        except subprocess.CalledProcessError as e:
            if kwargs.get("check", True):
                raise
            return subprocess.CompletedProcess(
                args, e.returncode, stdout=b"", stderr=e.stderr
            )
        return subprocess.CompletedProcess(args, 0, stdout=out, stderr=b"")

    def output(cmd: Iterable[Any], **kwargs: Any) -> str:
        args = [str(c) for c in cmd]
        if args[0] != "gh":
            return real_output(args, **kwargs)
        return gh(args, stdin(kwargs)).rstrip()

    mocker.patch("stack_pr.cli.run_shell_command", side_effect=run)
    mocker.patch("stack_pr.cli.get_command_output", side_effect=output)
    return gh
