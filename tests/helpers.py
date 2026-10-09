"""Plain builders shared by several test modules (fixtures live in conftest)."""

from __future__ import annotations

import json
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


PR_URL = "https://github.com/o/r/pull/{}"


def stack_info(pr_number: int, branch: str) -> str:
    """The stack-info trailer stack-pr writes into a submitted commit."""
    return f"stack-info: PR: {PR_URL.format(pr_number)}, branch: {branch}"


def init_stack_repo(
    tmp_path: Path, n: int, *, submitted: bool, user: str = "TestBot"
) -> tuple[Path, Path]:
    """A clone of a bare 'origin', checked out on 'feature' with *n* commits.

    Commit i (1-based) is titled "c<i>", has the body "Body of c<i>." and adds
    file<i>.txt. With *submitted*, each commit carries a stack-info trailer for
    PR #i on branch <user>/stack/<i>, and that branch is pushed to origin.

    Returns (local, remote).
    """
    remote = tmp_path / "remote.git"
    local = tmp_path / "local"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(remote))
    init_repo(local)
    git(local, "remote", "add", "origin", str(remote))
    git(local, "push", "-q", "origin", "main:refs/heads/main")
    git(local, "fetch", "-q", "origin")
    git(local, "checkout", "-q", "-b", "feature")
    for i in range(1, n + 1):
        (local / f"file{i}.txt").write_text(f"{i}\n")
        git(local, "add", f"file{i}.txt")
        msg = f"c{i}\n\nBody of c{i}."
        if submitted:
            msg += "\n\n" + stack_info(i, f"{user}/stack/{i}")
        git(local, "commit", "-q", "-m", msg)
        if submitted:
            git(local, "push", "-q", "origin", f"HEAD:refs/heads/{user}/stack/{i}")
    return local, remote


def commit_message(repo: Path, rev: str) -> str:
    """The full message of *rev*, without git's trailing newline."""
    return git(repo, "log", "-1", "--format=%B", rev).rstrip("\n")


def branches(repo: Path, pattern: str = "refs/heads") -> set[str]:
    """Short names of the refs under *pattern* in *repo*."""
    out = git(repo, "for-each-ref", pattern, "--format=%(refname:short)")
    return set(out.split())


GH_MERGE_QUEUE_BASE_ERR = (
    b"GraphQL: Cannot change the base branch because the branch has been "
    b"added to a merge queue. (updatePullRequest)\n"
)


@dataclass
class FakeGitHub:
    """Stands in for the `gh` CLI: serves PRs from memory, records commands.

    A failing command raises CalledProcessError carrying gh's stderr.

    `gh pr merge` squash-merges the PR's head branch into its base in *remote*
    (a bare repo), as GitHub would.
    """

    remote: Path | None = None
    prs: dict[int, dict[str, Any]] = field(default_factory=dict)
    calls: list[tuple[list[str], str | None]] = field(default_factory=list)

    def add_pr(
        self,
        number: int,
        *,
        head: str,
        base: str = "main",
        state: str = "OPEN",
        merge_state: str = "CLEAN",
        queued: bool = False,
    ) -> str:
        url = PR_URL.format(number)
        self.prs[number] = {
            "number": number,
            "url": url,
            "headRefName": head,
            "baseRefName": base,
            "state": state,
            "mergeStateStatus": merge_state,
            "title": "",
            "body": "",
            "queued": queued,
        }
        return url

    @property
    def commands(self) -> list[list[str]]:
        return [cmd for cmd, _ in self.calls]

    def mutations(self) -> list[list[str]]:
        """The commands issued, minus read-only `gh pr view` lookups."""
        return [cmd for cmd in self.commands if cmd[1:3] != ["pr", "view"]]

    def _pr(self, ref: str) -> dict[str, Any]:
        return self.prs[int(ref.rsplit("/", 1)[-1])]

    def __call__(self, cmd: list[str], stdin: str | None) -> str:
        """Run a gh command; return its stdout."""
        self.calls.append((cmd, stdin))
        sub, args = cmd[1:3], cmd[3:]
        if sub == ["pr", "view"] and args[1] == "--json":
            return json.dumps(self._pr(args[0]))
        if sub == ["pr", "edit"] and args[1] == "-B":
            pr = self._pr(args[0])
            if pr["queued"]:
                # What gh reports when GitHub refuses to retarget a queued PR.
                raise subprocess.CalledProcessError(
                    1, cmd, stderr=GH_MERGE_QUEUE_BASE_ERR
                )
            pr["baseRefName"] = args[2]
            return ""
        if sub == ["pr", "merge"] and args[1:3] == ["--squash", "-t"]:
            self._squash_merge(self._pr(args[0]), args[3], stdin or "")
            return ""
        if sub == ["pr", "create"]:
            base, head = args[args.index("-B") + 1], args[args.index("-H") + 1]
            number = max(self.prs, default=0) + 1
            return self.add_pr(number, head=head, base=base) + "\n"
        raise AssertionError(f"unexpected gh command: {cmd}")

    def _squash_merge(self, pr: dict[str, Any], title: str, body: str) -> None:
        pr["state"] = "MERGED"
        if self.remote is None:
            return
        base, head = pr["baseRefName"], pr["headRefName"]
        tree, parent = f"{head}^{{tree}}", f"refs/heads/{base}"
        msg = f"{title}\n\n{body}"
        # The bare remote has no identity configured, and CI none globally.
        identity = ["-c", "user.name=GitHub", "-c", "user.email=noreply@github.com"]
        sha = git(
            self.remote, *identity, "commit-tree", tree, "-p", parent, "-m", msg
        ).strip()
        git(self.remote, "update-ref", f"refs/heads/{base}", sha)
