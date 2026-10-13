"""The test isolation guard (``isolated_env`` in conftest.py) does its job."""

from __future__ import annotations

import os
import pwd
import subprocess
from pathlib import Path

import pytest

from tests.conftest import GH_SHIM_MESSAGE, IsolatedEnv

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_home_is_not_the_real_home(isolated_env: IsolatedEnv) -> None:
    real_home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    assert Path.home() == isolated_env.home
    assert Path.home() != real_home
    assert Path("~/.stack-pr").expanduser().is_relative_to(isolated_env.home)


def test_gh_has_no_credentials() -> None:
    for name in ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN"):
        assert os.environ[name] == ""
    assert not any(Path(os.environ["GH_CONFIG_DIR"]).iterdir())


def test_cwd_is_not_the_checkout(isolated_env: IsolatedEnv) -> None:
    assert Path.cwd() == isolated_env.cwd
    assert not Path.cwd().resolve().is_relative_to(REPO_ROOT)


@pytest.mark.parametrize(
    ("url", "transport"),
    [
        ("https://github.com/micahjsmith/stack-pr.git", "https"),
        ("git@github.com:micahjsmith/stack-pr.git", "ssh"),
    ],
)
def test_git_cannot_reach_network_remotes(url: str, transport: str) -> None:
    result = subprocess.run(
        ["git", "ls-remote", url],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode != 0
    assert f"transport '{transport}' not allowed" in result.stderr


# The guard, run in an inner pytest session.
_INNER_CONFTEST = "from tests.conftest import isolated_env  # noqa: F401\n"
_INNER_INI = "[pytest]\nmarkers =\n    allow_gh_shim: may run the gh shim\n"
_INNER_TESTS = """
import subprocess

import pytest


def test_runs_gh():
    result = subprocess.run(["gh", "auth", "status"], capture_output=True, check=False)
    assert result.returncode != 0  # caught: the guard must still fail the test


@pytest.mark.allow_gh_shim
def test_runs_gh_on_purpose(isolated_env):
    subprocess.run(["gh", "pr", "list"], capture_output=True, check=False)
    assert isolated_env.gh_calls() == ["gh pr list"]


def test_leaves_gh_alone():
    pass
"""


def test_guard_fails_a_test_that_runs_gh(pytester: pytest.Pytester) -> None:
    pytester.makeconftest(_INNER_CONFTEST)
    pytester.makeini(_INNER_INI)
    pytester.makepyfile(test_inner=_INNER_TESTS)

    result = pytester.runpytest("-p", "no:cacheprovider")

    result.assert_outcomes(passed=3, errors=1)
    result.stdout.fnmatch_lines(
        [
            "*ERROR at teardown of test_runs_gh*",
            f"*{GH_SHIM_MESSAGE}, but this test ran it 1 time(s):*",
            "*gh auth status*",
        ]
    )
