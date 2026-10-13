"""Shared fixtures, and the guard that keeps every test away from the real world.

The guard (``isolated_env``, autouse) exists because a test once reached real
GitHub with the developer's credentials and landed PRs. For every test it:

- points ``HOME`` (and ``XDG_CONFIG_HOME``) at a fresh temporary directory, so
  ``~/.stack-pr`` state and ``~/.gitconfig`` are never the developer's, and
  ignores the system git config;
- empties ``GH_TOKEN`` / ``GITHUB_TOKEN`` / ``GH_ENTERPRISE_TOKEN`` /
  ``GITHUB_ENTERPRISE_TOKEN`` and points ``GH_CONFIG_DIR`` at an empty
  directory, so gh has no credentials;
- sets ``GIT_ALLOW_PROTOCOL=file``, so git can only reach local (bare repo)
  remotes, never https/ssh;
- puts a ``gh`` shim first on ``PATH`` that logs its arguments and exits 1
  with "tests must not run the real gh". If a test invokes it, the test
  errors at teardown, even if it caught the failure. Tests route gh through
  ``fake_gh`` / ``FakeGitHub`` instead;
- starts the test in an empty temporary directory, not the checkout, so code
  that acts on "the current repo" finds no repo unless the test made one and
  ``chdir``-ed into it.

Opt-out: a test that legitimately runs ``gh`` and expects it to fail (e.g.
behavior when gh errors) marks itself ``@pytest.mark.allow_gh_shim``; it still
runs the shim, never the real gh. Request ``isolated_env`` to read the shim's
log (``isolated_env.gh_calls()``).
"""

from __future__ import annotations

import os
import stat
import subprocess
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

from stack_pr import cli, shell_commands
from stack_pr.git import git_config
from stack_pr.shell_commands import CommandFailedError
from tests.helpers import FakeGitHub, FakeShell

# For tests/test_isolation.py, which runs the guard in an inner pytest session.
pytest_plugins = ["pytester"]

GH_SHIM_MESSAGE = "tests must not run the real gh"

# Variables that would point git at another repository or config, or hand gh
# a host; dropped for every test.
_UNSET_ENV = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_COMMON_DIR",
    "GIT_CONFIG_GLOBAL",
    "GIT_CONFIG_SYSTEM",
    "GH_HOST",
    "GH_REPO",
)
_EMPTY_ENV = (
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "GH_ENTERPRISE_TOKEN",
    "GITHUB_ENTERPRISE_TOKEN",
)


@dataclass(frozen=True)
class IsolatedEnv:
    home: Path
    cwd: Path
    gh_log: Path

    def gh_calls(self) -> list[str]:
        """The argv of each run of the gh shim, one string per run."""
        if not self.gh_log.exists():
            return []
        return self.gh_log.read_text().splitlines()


def _write_gh_shim(bin_dir: Path, log: Path) -> None:
    shim = bin_dir / "gh"
    shim.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"gh $*\" >> '{log}'\n"
        f"echo '{GH_SHIM_MESSAGE}' >&2\n"
        "exit 1\n"
    )
    shim.chmod(shim.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


@pytest.fixture(autouse=True)
def isolated_env(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[IsolatedEnv]:
    """Isolate the test from the developer's HOME, gh auth, the network, and
    the checkout. See the module docstring."""
    base = tmp_path_factory.mktemp("isolated")
    env = IsolatedEnv(home=base / "home", cwd=base / "cwd", gh_log=base / "gh.log")
    bin_dir = base / "bin"
    for d in (env.home, env.cwd, bin_dir, base / "gh-config"):
        d.mkdir()
    _write_gh_shim(bin_dir, env.gh_log)

    for name in _UNSET_ENV:
        monkeypatch.delenv(name, raising=False)
    for name in _EMPTY_ENV:
        monkeypatch.setenv(name, "")
    monkeypatch.setenv("HOME", str(env.home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(env.home / ".config"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GH_CONFIG_DIR", str(base / "gh-config"))
    monkeypatch.setenv("GIT_ALLOW_PROTOCOL", "file")
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.chdir(env.cwd)

    yield env

    calls = env.gh_calls()
    if calls and request.node.get_closest_marker("allow_gh_shim") is None:
        pytest.fail(
            f"{GH_SHIM_MESSAGE}, but this test ran it {len(calls)} time(s):\n  "
            + "\n  ".join(calls)
            + "\nRoute gh through the fake_gh fixture (or FakeGitHub), or mark "
            "the test @pytest.mark.allow_gh_shim if it means to see gh fail.",
            pytrace=False,
        )


@pytest.fixture
def autoland_console(mocker) -> Mock:  # noqa: ANN001
    """Silence autoland's console; the mock scripts input and records output."""
    return mocker.patch("stack_pr.autoland.runtime.console")


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
    """Route stack-pr's `gh` commands to a FakeGitHub; everything else runs for real.

    Set the fixture's `remote` to a bare repo to have merges land there.
    """
    gh = FakeGitHub()
    real_run, real_output = cli.run_shell_command, cli.get_command_output
    real_run_with_retry = shell_commands.run_with_retry

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

    def run_with_retry(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        if cmd[0] != "gh":
            return real_run_with_retry(cmd, **kwargs)
        try:
            out = gh(cmd, stdin({"input": kwargs.get("input_data")}))
        except subprocess.CalledProcessError as e:
            result = subprocess.CompletedProcess(
                cmd, e.returncode, stdout="", stderr=(e.stderr or b"").decode()
            )
            if kwargs.get("check", True):
                raise CommandFailedError(result) from e
            return result
        return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")

    mocker.patch("stack_pr.cli.run_shell_command", side_effect=run)
    mocker.patch("stack_pr.cli.get_command_output", side_effect=output)
    # The shared gh JSON helpers (stack_pr.github) run gh through this.
    mocker.patch("stack_pr.shell_commands.run_with_retry", side_effect=run_with_retry)
    return gh
