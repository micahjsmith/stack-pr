"""Plain builders shared by several test modules (fixtures live in conftest)."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import Mock

from stack_pr.cli import CommonArgs


def git(cwd: Path, *args: str, check: bool = True) -> str:
    """Run git in *cwd* and return its stdout."""
    return subprocess.run(
        ["git", *args],  # noqa: S607
        cwd=cwd,
        check=check,
        capture_output=True,
        text=True,
    ).stdout


def init_repo(path: Path, *, content: str = "base\n") -> Path:
    """Create a git repo at *path* on 'main' with one commit of file.txt."""
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q", "-b", "main")
    git(path, "config", "user.name", "Test")
    git(path, "config", "user.email", "test@example.com")
    git(path, "config", "commit.gpgsign", "false")
    (path / "file.txt").write_text(content)
    git(path, "add", "file.txt")
    git(path, "commit", "-q", "-m", "initial")
    return path


def common_args(**overrides: object) -> CommonArgs:
    """CommonArgs as the CLI builds them with the default config."""
    base: dict = {
        "base": "main",
        "head": "HEAD",
        "remote": "origin",
        "target": "main",
        "hyperlinks": False,
        "verbose": False,
        "branch_name_template": "$USERNAME/stack/$ID",
        "show_tips": False,
        "land_disabled": False,
    }
    base.update(overrides)
    return CommonArgs(**base)


def mock_entry(
    pr_number: int | None = None,
    *,
    head: str | None = None,
    base: str | None = None,
    title: str = "",
    commit_msg: str | None = None,
    commit_id: str = "abc123",
) -> Mock:
    """A stand-in for a cli.StackEntry, with its commit's accessors stubbed."""
    e = Mock()
    e.pr = f"https://github.com/o/r/pull/{pr_number}" if pr_number is not None else None
    e.has_pr.return_value = pr_number is not None
    e.head = head
    e.base = base
    e.has_base.return_value = base is not None
    e.commit.title.return_value = title
    e.commit.commit_msg.return_value = title if commit_msg is None else commit_msg
    e.commit.commit_id.return_value = commit_id
    return e


@dataclass
class FakeShell:
    """Stands in for run_shell_command: records commands, replays results.

    Each command gets the next scripted result, or success once they run out.
    Like the real thing, a failure raises unless the caller passed check=False.
    """

    calls: list[tuple[list[str], dict[str, Any]]] = field(default_factory=list)
    results: list[tuple[int, bytes]] = field(default_factory=list)

    def script(self, *results: tuple[int, bytes]) -> None:
        """Queue (returncode, stderr) results for the next commands."""
        self.results.extend(results)

    @property
    def commands(self) -> list[list[str]]:
        return [cmd for cmd, _ in self.calls]

    def __call__(self, cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        self.calls.append((list(cmd), kwargs))
        returncode, stderr = self.results.pop(0) if self.results else (0, b"")
        if returncode and kwargs.get("check", True):
            raise subprocess.CalledProcessError(returncode, cmd, stderr=stderr)
        return subprocess.CompletedProcess(cmd, returncode, stdout=b"", stderr=stderr)
