# stack-pr autoland: land a whole stack through the GitHub merge queue.
#
# This module holds the full autoland engine. cli.py only wires up the
# subparser and dispatches into `run_autoland` to keep cli.py small.
#
# Repo-specific behavior (which CI checks gate a merge, poll intervals,
# retry counts, workflow timeouts, and whether the repo uses a merge queue at
# all) is externalized to the `[autoland]` config section and command-line
# flags, so a repo can reproduce its workflow with configuration alone.
from __future__ import annotations

import argparse
import configparser
import contextlib
import fcntl
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import Union

# FIXME(stack-pr): autoland reaches into cli for shared building blocks
# (get_stack, command_submit, last) and reimplements a retrying subprocess
# wrapper below (run/gh_json) that overlaps with stack_pr.shell_commands. These
# should be consolidated into a shared module (e.g. stack_pr.git / a common
# helpers module) usable by every subcommand, rather than importing from cli.
from stack_pr import cli

# ---------------------------------------------------------------------------
# Defaults (overridable via [autoland] config or flags)
# ---------------------------------------------------------------------------

DEFAULT_POLL_INTERVAL = 120  # seconds
DEFAULT_MAX_CHECK_RETRIES = 3
DEFAULT_MAX_QUEUE_RETRIES = 3
DEFAULT_WORKFLOW_TIMEOUT = 10800  # 3 hours
DEFAULT_MERGE_TIMEOUT = 3600  # 60 minutes

# ---------------------------------------------------------------------------
# Output: use rich when available, fall back to plain text otherwise.
# ---------------------------------------------------------------------------

# Matches rich-style markup tags like [bold], [/dim], [red bold] so the
# plain-text console can strip them.
_RE_MARKUP = re.compile(r"\[/?[a-z][a-z0-9 _]*\]")


class _PlainConsole:
    """Minimal stand-in for rich.Console that strips markup."""

    def print(self, *args: object, **_kwargs: object) -> None:
        print(*[_RE_MARKUP.sub("", str(a)) for a in args])

    def input(self, prompt: object = "") -> str:
        return input(_RE_MARKUP.sub("", str(prompt)))


try:
    from rich.console import Console
    from rich.text import Text

    HAVE_RICH = True
except ImportError:  # pragma: no cover - exercised only without the extra
    Console = _PlainConsole  # type: ignore[assignment,misc]
    Text = None  # type: ignore[assignment,misc]
    HAVE_RICH = False

console = Console()


# ---------------------------------------------------------------------------
# Options resolved by precedence: command-line flag, then config, then default
# ---------------------------------------------------------------------------


@dataclass
class AutolandOptions:
    merge_queue: bool
    required_checks: list[str]
    poll_interval: int
    max_check_retries: int
    max_queue_retries: int
    merge_timeout: int
    workflow_timeout: int
    default_workflow: str | None
    count: int | None
    dry_run: bool
    branch: str | None
    interactive: bool
    resume: bool
    state_file: Path | None
    always_cleanup: bool
    plan_file: Path | None = None

    @classmethod
    def from_config_and_args(
        cls, config: configparser.ConfigParser, args: argparse.Namespace
    ) -> AutolandOptions:
        def _int(flag_val: int | None, key: str, default: int) -> int:
            if flag_val is not None:
                return flag_val
            return config.getint("autoland", key, fallback=default)

        raw_checks = config.get("autoland", "required_checks", fallback="")
        required_checks = [c.strip() for c in raw_checks.split(",") if c.strip()]

        state_file = getattr(args, "state_file", None)
        # Resolve the plan file to an absolute path now, while the cwd is still
        # the user's invocation directory: autoland may later chdir into a
        # temporary worktree (--branch), where a relative path would not resolve.
        plan_file = getattr(args, "plan_file", None)
        return cls(
            merge_queue=config.getboolean("autoland", "merge_queue", fallback=False),
            required_checks=required_checks,
            poll_interval=_int(
                getattr(args, "poll_interval", None),
                "poll_interval",
                DEFAULT_POLL_INTERVAL,
            ),
            max_check_retries=_int(
                getattr(args, "max_check_retries", None),
                "max_check_retries",
                DEFAULT_MAX_CHECK_RETRIES,
            ),
            max_queue_retries=_int(
                getattr(args, "max_queue_retries", None),
                "max_queue_retries",
                DEFAULT_MAX_QUEUE_RETRIES,
            ),
            merge_timeout=config.getint(
                "autoland", "merge_timeout", fallback=DEFAULT_MERGE_TIMEOUT
            ),
            workflow_timeout=_int(
                getattr(args, "workflow_timeout", None),
                "workflow_timeout",
                DEFAULT_WORKFLOW_TIMEOUT,
            ),
            default_workflow=(
                config.get("autoland", "default_workflow", fallback="").strip() or None
            ),
            count=getattr(args, "count", None),
            dry_run=bool(getattr(args, "dry_run", False)),
            branch=getattr(args, "branch", None),
            interactive=bool(getattr(args, "interactive", False)),
            resume=bool(getattr(args, "resume", False)),
            state_file=Path(state_file) if state_file else None,
            always_cleanup=bool(getattr(args, "always_cleanup", False)),
            plan_file=Path(plan_file).resolve() if plan_file else None,
        )


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


class PRState(str, Enum):
    PENDING = "pending"
    WAITING_FOR_APPROVAL = "waiting_for_approval"
    WAITING_FOR_CHECKS = "waiting_for_checks"
    IN_MERGE_QUEUE = "in_merge_queue"
    WAITING_FOR_WORKFLOW = "waiting_for_workflow"
    MERGED = "merged"
    FAILED = "failed"


@dataclass
class StackEntry:
    """One PR in the stack (bottom = index 0)."""

    pr_url: str
    pr_number: int
    branch: str
    title: str = ""
    review_decision: str = ""  # APPROVED, REVIEW_REQUIRED, CHANGES_REQUESTED, ""
    state: PRState = PRState.PENDING
    check_retries: int = 0
    queue_retries: int = 0
    error_message: str = ""

    @property
    def is_approved(self) -> bool:
        return self.review_decision == "APPROVED"


@dataclass
class LandStep:
    """Land a PR in the stack through the merge queue.

    ``entry_index`` indexes into ``LandingContext.stack``, or is ``-1`` for a
    PR that has already landed: such a PR is no longer part of the stack, so
    the step is skipped at execution time.

    ``pr_number`` is set when the plan pinned a specific PR (``l 123``) and is
    ``None`` for a bare, positional ``l``. It is the only thing identifying an
    already-landed step, so it is always set when ``entry_index`` is ``-1``.
    """

    entry_index: int
    pr_number: int | None = None

    @property
    def already_landed(self) -> bool:
        return self.entry_index < 0


@dataclass
class WorkflowStep:
    """Wait for a GitHub Actions workflow to succeed with the landed code."""

    workflow: str
    state: str = "pending"  # pending, waiting, succeeded, failed, skipped
    error_message: str = ""


@dataclass
class ConfirmStep:
    """Pause for manual confirmation before continuing.

    ``condition`` is an optional human-readable thing to verify before
    proceeding (e.g. ``"QA sign-off complete"``). When set, it is shown in the
    confirmation prompt; when empty, a generic prompt is shown. Either way the
    step waits until the user types ``y``/``Y`` and presses Enter.
    """

    condition: str = ""
    confirmed: bool = False


PlanStep = Union[LandStep, WorkflowStep, ConfirmStep]


@dataclass
class LandingContext:
    """Mutable state for the landing run."""

    stack: list[StackEntry] = field(default_factory=list)
    plan: list[PlanStep] = field(default_factory=list)
    current_step: int = 0
    current_index: int = 0  # index into stack for the active land step
    aborted: bool = False
    abort_reason: str = ""
    last_landed_sha: str = ""  # merge commit of the last landed PR


# ---------------------------------------------------------------------------
# State persistence (checkpoint / resume)
# ---------------------------------------------------------------------------

STATE_VERSION = 1

_STEP_TYPES = {LandStep: "land", WorkflowStep: "workflow", ConfirmStep: "confirm"}


def _deserialize_entry(data: dict) -> StackEntry:
    return StackEntry(
        pr_url=data["pr_url"],
        pr_number=data["pr_number"],
        branch=data["branch"],
        title=data.get("title", ""),
        review_decision=data.get("review_decision", ""),
        state=PRState(data.get("state", "pending")),
        check_retries=data.get("check_retries", 0),
        queue_retries=data.get("queue_retries", 0),
        error_message=data.get("error_message", ""),
    )


def _serialize_step(step: PlanStep) -> dict:
    # dataclasses.asdict gives the fields; tag the type for deserialization.
    return {"type": _STEP_TYPES[type(step)], **asdict(step)}


def _deserialize_step(data: dict) -> PlanStep:
    fields = {k: v for k, v in data.items() if k != "type"}
    if data["type"] == "land":
        return LandStep(**fields)
    if data["type"] == "workflow":
        return WorkflowStep(**fields)
    return ConfirmStep(**fields)


@dataclass
class AutolandCheckpointer:
    """Persists and restores landing state for crash-safe ``--resume``."""

    path: Path
    branch: str
    base: str

    @staticmethod
    def default_path(branch: str) -> Path:
        slug = re.sub(r"[^a-zA-Z0-9_-]", "_", branch)
        return Path.home() / ".stack-pr" / "autoland" / f"{slug}.json"

    def save(self, ctx: LandingContext) -> None:
        """Atomically write a checkpoint of *ctx* to the state file."""
        data = {
            "version": STATE_VERSION,
            "branch": self.branch,
            "base": self.base,
            "current_step": ctx.current_step,
            "last_landed_sha": ctx.last_landed_sha,
            "stack": [asdict(e) for e in ctx.stack],
            "plan": [_serialize_step(s) for s in ctx.plan],
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2) + "\n")
        tmp.rename(self.path)

    def delete(self) -> None:
        self.path.unlink(missing_ok=True)

    @classmethod
    def load(cls, path: Path) -> tuple[AutolandCheckpointer, LandingContext]:
        """Load a checkpoint; returns the checkpointer and restored context."""
        data = json.loads(path.read_text())
        if data.get("version") != STATE_VERSION:
            raise ValueError(f"Unsupported state file version: {data.get('version')}")
        ctx = LandingContext(
            stack=[_deserialize_entry(e) for e in data["stack"]],
            plan=[_deserialize_step(s) for s in data["plan"]],
            current_step=data.get("current_step", 0),
            last_landed_sha=data.get("last_landed_sha", ""),
        )
        return cls(path=path, branch=data["branch"], base=data["base"]), ctx


@dataclass
class AutolandLock:
    """Advisory filesystem lock preventing concurrent autolands on a branch.

    The lock is an ``flock`` held for the lifetime of the process, so the OS
    releases it automatically on exit — including crashes. A failed or
    interrupted run therefore frees the lock (so it can later be resumed) while
    its state file persists. The lock *file* is only a handle: a leftover file
    from a crashed run does not block a future run, because acquisition depends
    on the flock, not on the file's existence.
    """

    path: Path
    _fd: int | None = field(default=None, repr=False)

    @staticmethod
    def for_state(state_path: Path) -> AutolandLock:
        """Return the lock sitting next to *state_path* (``<name>.lock``)."""
        return AutolandLock(state_path.with_name(state_path.name + ".lock"))

    def acquire(self) -> bool:
        """Try to take the lock; return False if another process holds it."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        self._fd = fd
        return True

    def release(self) -> None:
        """Release the lock and remove the lock file (no-op if not held)."""
        if self._fd is None:
            return
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        finally:
            os.close(self._fd)
            self._fd = None
            self.path.unlink(missing_ok=True)


def _current_branch() -> str:
    return run(["git", "rev-parse", "--abbrev-ref", "HEAD"], quiet=True).stdout.strip()


# ---------------------------------------------------------------------------
# Sleep / wake resilience
# ---------------------------------------------------------------------------

_SLEEP_DETECTION_THRESHOLD = 30


def resilient_sleep(seconds: int) -> float:
    """Sleep, detecting system sleep/wake; wait for network after a wake."""
    start = time.monotonic()
    time.sleep(seconds)
    actual = time.monotonic() - start

    sleep_gap = max(0.0, actual - seconds - _SLEEP_DETECTION_THRESHOLD)
    if sleep_gap > 0:
        gap_min = int(sleep_gap) // 60
        gap_sec = int(sleep_gap) % 60
        console.print(
            f"\n[yellow]System sleep detected — machine was suspended "
            f"~{gap_min}m{gap_sec}s. Waiting for network...[/yellow]"
        )
        _wait_for_network()
        console.print("[green]Network is back. Resuming.[/green]\n")

    return sleep_gap


def _wait_for_network(max_wait: int = 120, interval: int = 5) -> None:
    """Block until ``gh api user`` succeeds or *max_wait* seconds elapse."""
    deadline = time.monotonic() + max_wait
    while time.monotonic() < deadline:
        try:
            result = subprocess.run(
                ["gh", "api", "user", "--jq", ".login"],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            if result.returncode == 0:
                return
        except (subprocess.TimeoutExpired, OSError):
            pass
        time.sleep(interval)
    console.print(
        f"[yellow]Warning: network still unreachable after {max_wait}s — "
        "continuing anyway[/yellow]"
    )


# ---------------------------------------------------------------------------
# Shell helpers (with transient-failure retries)
# ---------------------------------------------------------------------------

_MAX_RETRIES = 2
_RETRY_DELAY = 10


def run(
    cmd: list[str],
    *,
    check: bool = True,
    capture: bool = True,
    quiet: bool = False,
    input_data: bytes | None = None,
    retries: int = _MAX_RETRIES,
) -> subprocess.CompletedProcess[str]:
    """Run a subprocess, retrying likely-transient failures.

    Commands run in the current working directory (autoland chdirs into a
    temporary worktree when ``--branch`` is used).
    """
    last_err: Exception | None = None

    for attempt in range(retries + 1):
        if attempt > 0:
            if not quiet:
                console.print(
                    f"[yellow]  retry {attempt}/{retries} in {_RETRY_DELAY}s..."
                    "[/yellow]"
                )
            time.sleep(_RETRY_DELAY)

        if not quiet:
            suffix = "" if attempt == 0 else f"  (attempt {attempt + 1})"
            console.print(f"[dim]$ {' '.join(cmd)}{suffix}[/dim]")

        try:
            result = subprocess.run(
                cmd,
                capture_output=capture,
                text=True,
                input=input_data.decode() if input_data else None,
                timeout=300,
                check=False,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            last_err = RuntimeError(f"Command error: {exc}")
            continue

        if check and result.returncode != 0:
            stderr = result.stderr.strip() if result.stderr else ""
            last_err = RuntimeError(
                f"Command failed ({result.returncode}): {' '.join(cmd)}\n{stderr}"
            )
            # Only retry failures that look transient (network), not logical
            # failures like a merge conflict.
            if _is_likely_transient(result):
                continue
            raise last_err

        return result

    assert last_err is not None
    raise last_err


def _is_likely_transient(result: subprocess.CompletedProcess[str]) -> bool:
    indicators = [
        "could not resolve",
        "connection refused",
        "connection reset",
        "timed out",
        "timeout",
        "network is unreachable",
        "temporary failure",
        "ssl",
        "eof",
        "broken pipe",
        "http 5",  # 500, 502, 503, etc.
        "server error",
        "try again",
        "unavailable",
    ]
    text = ((result.stderr or "") + (result.stdout or "")).lower()
    return any(ind in text for ind in indicators)


def gh_json(cmd: list[str]) -> dict | list:
    """Run a gh command and parse JSON output."""
    result = run(["gh", *cmd], quiet=True)
    return json.loads(result.stdout)


# ---------------------------------------------------------------------------
# GitHub access — every `gh` / `gh api` call autoland makes lives here.
# ---------------------------------------------------------------------------

_MERGE_QUEUE_ENTRY_QUERY = """
query($owner: String!, $repo: String!, $number: Int!) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $number) {
      mergeQueueEntry { id state }
    }
  }
}
""".strip()


@dataclass
class MergeQueuePollResult:
    merged: bool = False
    booted: bool = False
    error: str = ""


class GitHub:
    """Wrapper over the ``gh`` CLI for the PR / merge-queue calls autoland needs."""

    def __init__(self) -> None:
        self._owner_repo: tuple[str, str] | None = None

    def owner_repo(self) -> tuple[str, str]:
        if self._owner_repo is None:
            data = gh_json(["repo", "view", "--json", "owner,name"])
            assert isinstance(data, dict)
            self._owner_repo = (data["owner"]["login"], data["name"])
        return self._owner_repo

    def _pr_view(self, pr_number: int, fields: str) -> dict:
        data = gh_json(["pr", "view", str(pr_number), "--json", fields])
        assert isinstance(data, dict)
        return data

    def pr_state(self, pr_number: int) -> str:
        return self._pr_view(pr_number, "state").get("state", "OPEN")

    def merge_state(self, pr_number: int) -> dict:
        return self._pr_view(pr_number, "state,mergeStateStatus,mergeable")

    def review_decision(self, pr_number: int) -> str:
        return self._pr_view(pr_number, "reviewDecision").get("reviewDecision", "")

    def summary(self, pr_number: int) -> dict:
        return self._pr_view(pr_number, "title,state,reviewDecision")

    def checks(self, pr_number: int) -> list[dict]:
        data = gh_json(
            [
                "pr",
                "checks",
                str(pr_number),
                "--json",
                "name,state,bucket,link,workflow",
            ]
        )
        assert isinstance(data, list)
        return data

    def rerun_failed(self, run_ids: list[int]) -> None:
        for run_id in dict.fromkeys(run_ids):  # de-dup, preserve order
            try:
                run(["gh", "run", "rerun", str(run_id), "--failed"], quiet=False)
            except RuntimeError as e:
                console.print(
                    f"[yellow]Warning: could not rerun {run_id}: {e}[/yellow]"
                )

    def in_merge_queue(self, pr_number: int) -> bool:
        """Whether the PR currently has an active merge-queue entry (GraphQL)."""
        try:
            owner, repo = self.owner_repo()
            result = run(
                [
                    "gh",
                    "api",
                    "graphql",
                    "-F",
                    f"owner={owner}",
                    "-F",
                    f"repo={repo}",
                    "-F",
                    f"number={pr_number}",
                    "-f",
                    f"query={_MERGE_QUEUE_ENTRY_QUERY}",
                ],
                quiet=True,
            )
            entry = (
                json.loads(result.stdout)
                .get("data", {})
                .get("repository", {})
                .get("pullRequest", {})
                .get("mergeQueueEntry")
            )
            return entry is not None
        except (RuntimeError, json.JSONDecodeError):
            return False

    def enqueue(self, pr_number: int) -> None:
        run(["gh", "pr", "merge", str(pr_number), "--squash"], quiet=False)

    def poll_merge(self, pr_number: int) -> MergeQueuePollResult:
        state = self.pr_state(pr_number)
        if state == "MERGED":
            return MergeQueuePollResult(merged=True)
        if state == "CLOSED":
            return MergeQueuePollResult(error="PR was closed")
        if state == "OPEN" and not self.in_merge_queue(pr_number):
            return MergeQueuePollResult(booted=True)
        return MergeQueuePollResult()

    def workflow_runs(self, workflow: str, branch: str) -> list[dict]:
        data = gh_json(
            [
                "run",
                "list",
                "--workflow",
                workflow,
                "--branch",
                branch,
                "--json",
                "headSha,status,conclusion",
                "--limit",
                "10",
            ]
        )
        assert isinstance(data, list)
        return data

    def merge_commit(self, pr_number: int) -> str | None:
        """Return the SHA of the commit ``pr_number`` merged as, if any."""
        try:
            data = gh_json(["pr", "view", str(pr_number), "--json", "mergeCommit"])
        except RuntimeError:
            return None
        if isinstance(data, dict):
            merge = data.get("mergeCommit")
            if isinstance(merge, dict):
                return merge.get("oid") or None
        return None


github = GitHub()


# ---------------------------------------------------------------------------
# Worktree management
# ---------------------------------------------------------------------------


class Worktree:
    """A temporary git worktree autoland operates in (for ``--branch``).

    ``create`` checks the branch out in a throwaway worktree and chdirs into
    it; ``remove`` restores the original directory and deletes the worktree.
    ``announce_preserved`` is used instead of ``remove`` to keep it around for
    debugging after a failure.
    """

    def __init__(self, branch: str) -> None:
        self.branch = branch
        self.path: Path | None = None
        self._orig_cwd: str | None = None

    def create(self) -> None:
        tmpdir = tempfile.mkdtemp(prefix="autoland-")
        worktree_dir = str(Path(tmpdir) / "repo")
        console.print(
            f"[bold]Creating temporary worktree for [cyan]{self.branch}[/cyan] "
            f"at {worktree_dir}[/bold]"
        )
        subprocess.run(
            ["git", "worktree", "add", "-f", worktree_dir, self.branch],
            check=True,
            capture_output=True,
            text=True,
        )
        self.path = Path(worktree_dir)
        self._orig_cwd = str(Path.cwd())
        os.chdir(worktree_dir)

    def remove(self) -> None:
        if self.path is None:
            return
        if self._orig_cwd:
            os.chdir(self._orig_cwd)
            self._orig_cwd = None
        console.print(f"\n[dim]Cleaning up worktree at {self.path}...[/dim]")
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(self.path)],
            check=False,
            capture_output=True,
            text=True,
        )
        shutil.rmtree(self.path.parent, ignore_errors=True)
        self.path = None

    def announce_preserved(self) -> None:
        if self.path is None:
            return
        console.print(
            f"\n[bold yellow]Worktree preserved at: "
            f"[cyan]{self.path}[/cyan][/bold yellow]"
        )
        console.print(
            f"[dim]To clean up manually: git worktree remove --force {self.path}[/dim]"
        )


# ---------------------------------------------------------------------------
# Stack discovery (reuses stack-pr's get_stack)
# ---------------------------------------------------------------------------


def discover_stack(common: cli.CommonArgs) -> list[StackEntry]:
    """Discover the stack via stack-pr's own parser, bottom-to-top order."""
    entries: list[StackEntry] = []
    for e in cli.get_stack(base=common.base, head=common.head, verbose=common.verbose):
        if not e.has_pr():
            continue  # commit not submitted yet — skip
        pr_number = int(cli.last(e.pr))
        entries.append(StackEntry(pr_url=e.pr, pr_number=pr_number, branch=e.head))
    return entries


def enrich_stack(stack: list[StackEntry]) -> None:
    """Fetch PR titles, review status, and current state from GitHub."""
    for entry in stack:
        try:
            data = github.summary(entry.pr_number)
            entry.title = data.get("title", "")
            entry.review_decision = data.get("reviewDecision", "")
            if data.get("state") == "MERGED":
                entry.state = PRState.MERGED
        except RuntimeError:
            entry.title = "(could not fetch)"


# ---------------------------------------------------------------------------
# Check monitoring
# ---------------------------------------------------------------------------

RE_RUN_ID_FROM_LINK = re.compile(r"/actions/runs/(\d+)")


def _extract_run_id(link: str) -> int | None:
    m = RE_RUN_ID_FROM_LINK.search(link)
    return int(m.group(1)) if m else None


class CheckStatus(Enum):
    ALL_PASSING = "all_passing"
    PENDING = "pending"
    FAILED = "failed"
    NOT_STARTED = "not_started"


@dataclass
class CheckResult:
    status: CheckStatus
    failed_runs: list[int] = field(default_factory=list)
    failed_names: list[str] = field(default_factory=list)
    summary: str = ""


def evaluate_checks(checks: list[dict], required_checks: list[str]) -> CheckResult:
    """Evaluate a PR's check runs.

    If *required_checks* is non-empty, gate on exactly those named checks.
    Otherwise gate on all reported checks that aren't being skipped.
    """
    if required_checks:
        check_map = {
            c.get("name", ""): c for c in checks if c.get("name", "") in required_checks
        }
        missing = [n for n in required_checks if n not in check_map]
        if missing:
            return CheckResult(
                status=CheckStatus.NOT_STARTED,
                summary=f"Waiting for checks to start: {', '.join(missing)}",
            )
        names = list(required_checks)
    else:
        check_map = {}
        for c in checks:
            if (c.get("bucket") or "").lower() == "skipping":
                continue
            check_map[c.get("name", "")] = c
        if not check_map:
            return CheckResult(
                status=CheckStatus.NOT_STARTED,
                summary="Waiting for checks to start",
            )
        names = list(check_map.keys())

    failed_runs: list[int] = []
    failed_names: list[str] = []
    any_pending = False

    for name in names:
        bucket = (check_map[name].get("bucket") or "").lower()
        if bucket == "pass":
            continue
        if bucket in ("fail", "cancel"):
            run_id = _extract_run_id(check_map[name].get("link", ""))
            if run_id:
                failed_runs.append(run_id)
            failed_names.append(name)
        else:
            # pending, skipping, or unknown -> still waiting
            any_pending = True

    if failed_names:
        return CheckResult(
            status=CheckStatus.FAILED,
            failed_runs=failed_runs,
            failed_names=failed_names,
            summary=f"Failed: {', '.join(failed_names)}",
        )
    if any_pending:
        return CheckResult(status=CheckStatus.PENDING, summary="Checks in progress...")
    return CheckResult(status=CheckStatus.ALL_PASSING, summary="All checks passing")


# ---------------------------------------------------------------------------
# Post-merge rebase + resubmit (reuses stack-pr submit)
# ---------------------------------------------------------------------------


def rebase_and_resubmit(common: cli.CommonArgs) -> None:
    """After a merge, rebase the local stack on the target and re-submit."""
    console.print(
        f"\n[bold]Rebasing stack on {common.remote}/{common.target}...[/bold]"
    )
    run(["git", "fetch", common.remote, common.target], quiet=False)
    # Rebase the current branch (don't name it) so this works even when the
    # branch is checked out in another worktree.
    run(["git", "rebase", f"{common.remote}/{common.target}"], quiet=False)

    # Re-deduce the base against the *current* origin/<target>. `common.base`
    # was deduced once when autoland started (merge-base with the target at
    # that time). After other PRs land in the target and we rebase onto it,
    # that cached base is stale: the range base..HEAD would then sweep in every
    # commit merged by others in the meantime, and submit would try to open
    # bogus PRs for them. Clearing the base forces deduce_base to recompute
    # merge-base(HEAD, origin/<target>) from the freshly rebased HEAD.
    resubmit_common = cli.deduce_base(replace(common, base=""))

    console.print("[bold]Re-submitting stack...[/bold]")
    cli.command_submit(
        resubmit_common,
        draft=False,
        reviewer="",
        keep_body=True,
        keep_title=True,
        draft_bitmask=None,
    )


# ---------------------------------------------------------------------------
# Workflow checkpoint polling
# ---------------------------------------------------------------------------


def _refresh_last_landed_sha(
    ctx: LandingContext, common: cli.CommonArgs, pr_number: int | None = None
) -> None:
    """Record the SHA of the most recently landed code.

    When ``pr_number`` is given, prefer that PR's exact merge commit so the
    workflow checkpoint targets the code we actually landed. In a busy repo,
    ``origin/<target>`` can advance past our merge commit (bot commits, other
    PRs) between the merge and this call, so using its HEAD would overshoot and
    make the checkpoint wait for a deploy of code that was never our own.
    """
    with contextlib.suppress(RuntimeError):  # fetch is non-critical
        run(["git", "fetch", common.remote, common.target], quiet=True)

    if pr_number is not None:
        merge_sha = github.merge_commit(pr_number)
        if merge_sha:
            ctx.last_landed_sha = merge_sha
            return

    try:
        result = run(
            ["git", "rev-parse", f"{common.remote}/{common.target}"], quiet=True
        )
        ctx.last_landed_sha = result.stdout.strip()
    except RuntimeError:
        pass  # non-critical; will retry when needed


def _is_ancestor(ancestor: str, descendant: str) -> bool:
    result = run(
        ["git", "merge-base", "--is-ancestor", ancestor, descendant],
        check=False,
        quiet=True,
    )
    return result.returncode == 0


def wait_for_workflow(
    step: WorkflowStep,
    *,
    opts: AutolandOptions,
    common: cli.CommonArgs,
    ctx: LandingContext,
) -> bool:
    """Wait for a workflow to complete with code at or after the landed SHA."""
    target_sha = ctx.last_landed_sha
    step.state = "waiting"
    console.print(
        f"\n[bold blue]Waiting for workflow: {step.workflow}[/bold blue]"
        f"\n[dim]Target SHA: {target_sha[:12]}[/dim]"
    )

    awake_elapsed = 0.0
    while True:
        if ctx.aborted:
            return False
        if awake_elapsed > opts.workflow_timeout:
            step.state = "failed"
            step.error_message = (
                f"Workflow timed out after {opts.workflow_timeout / 3600:.0f}h"
            )
            return False

        try:
            data = github.workflow_runs(step.workflow, common.target)
        except RuntimeError as e:
            console.print(f"[yellow]Warning: could not poll workflow: {e}[/yellow]")
            resilient_sleep(opts.poll_interval)
            awake_elapsed += opts.poll_interval
            continue

        for wf_run in data:
            if wf_run.get("status") != "completed":
                continue
            if wf_run.get("conclusion") != "success":
                continue
            run_sha = wf_run.get("headSha", "")
            if not run_sha:
                continue
            if run_sha == target_sha or _is_ancestor(target_sha, run_sha):
                step.state = "succeeded"
                step.error_message = ""
                console.print(
                    f"\n[bold green]Workflow {step.workflow} completed "
                    f"with SHA {run_sha[:12]}[/bold green]"
                )
                return True

        mins = int(awake_elapsed) // 60
        step.error_message = f"Waiting for workflow ({mins}m elapsed)..."
        console.print(
            f"[dim]Workflow {step.workflow}: waiting ({mins}m) — "
            f"polling in {opts.poll_interval}s[/dim]"
        )
        resilient_sleep(opts.poll_interval)
        awake_elapsed += opts.poll_interval


# ---------------------------------------------------------------------------
# Interactive plan editing
# ---------------------------------------------------------------------------


def generate_default_plan(
    stack: list[StackEntry],
    default_workflow: str | None = None,
    count: int | None = None,
) -> list[PlanStep]:
    # Land the bottom `count` PRs (the whole stack when count is None). Landing
    # goes bottom-to-top, so a partial land is always a prefix of the stack.
    n = len(stack) if count is None else count
    plan: list[PlanStep] = [
        LandStep(entry_index=i, pr_number=stack[i].pr_number) for i in range(n)
    ]
    # If a default workflow is configured, wait for it once those PRs have
    # landed. The user can still edit or remove this step in interactive mode.
    if default_workflow:
        plan.append(WorkflowStep(workflow=default_workflow))
    return plan


def format_plan_for_editor(stack: list[StackEntry], plan: list[PlanStep]) -> str:
    lines = [
        "# Autoland plan — edit steps below.",
        "# l [<pr>]        = land that PR (a number, or its URL); a bare 'l'",
        "#                   lands the next PR in the stack instead",
        "# w <workflow>    = wait for a workflow to complete",
        "# c [<condition>] = pause for manual confirmation; the optional",
        "#                   condition names what to verify before proceeding",
        "#                   (e.g. 'c QA sign-off complete')",
        "#",
        "# Lines starting with # are comments and are ignored.",
        "# Blank lines are ignored.",
        "#",
    ]
    for step in plan:
        if isinstance(step, LandStep):
            if step.already_landed:
                lines.append(f"l {step.pr_number}    # already landed")
                continue
            entry = stack[step.entry_index]
            comment = f"    # {entry.title}" if entry.title else ""
            lines.append(f"l {entry.pr_number}{comment}")
        elif isinstance(step, WorkflowStep):
            lines.append(f"w {step.workflow}")
        elif isinstance(step, ConfirmStep):
            lines.append(f"c {step.condition}".rstrip())
    lines.append("")
    return "\n".join(lines)


# A pinned PR reference: a bare number, or the PR's URL. The owner and repo are
# captured so a URL can be checked against the repo being landed into. Note that
# the '#123' spelling is deliberately unsupported: '#' starts a comment, so
# 'l #123' is indistinguishable from a bare 'l' with a comment after it.
RE_PR_URL = re.compile(r"^https?://[^/]+/([^/]+)/([^/]+)/pull/(\d+)/?$")


def _current_owner_repo() -> tuple[str, str]:
    """The repository autoland is landing into, as ``(owner, name)``."""
    try:
        return github.owner_repo()
    except (RuntimeError, KeyError, json.JSONDecodeError) as e:
        raise ValueError(f"Could not determine the current repository: {e}") from e


def _parse_pr_ref(ref: str, line_num: int) -> int:
    """Parse the argument of an ``l`` step into a PR number.

    A URL must point at the repository being landed into. Everything downstream
    — the merge check, the merge queue, the stack itself — is resolved against
    that single repo, so a PR elsewhere could not be landed even if its number
    were understood; saying so beats silently landing whatever PR happens to
    carry the same number here.
    """
    m = RE_PR_URL.match(ref)
    if m:
        owner, repo, number = m.groups()
        this_owner, this_repo = _current_owner_repo()
        # GitHub treats owner/repo case-insensitively, so a URL that differs
        # only in case still points at this repo.
        if (owner.lower(), repo.lower()) != (this_owner.lower(), this_repo.lower()):
            raise ValueError(
                f"Line {line_num}: {ref} points at {owner}/{repo}, but this "
                f"stack lands into {this_owner}/{this_repo} — landing PRs "
                "across repositories is not currently supported"
            )
        return int(number)
    if ref.isdigit():
        return int(ref)
    raise ValueError(
        f"Line {line_num}: 'l' takes a PR number or URL, not {ref!r} "
        "(use a bare 'l' to land the next PR in the stack)"
    )


def _pr_is_merged(pr_number: int) -> bool:
    """Whether GitHub reports *pr_number* as merged."""
    try:
        return github.pr_state(pr_number) == "MERGED"
    except RuntimeError as e:
        raise ValueError(f"Could not look up PR #{pr_number} on GitHub: {e}") from e


def _mark_skipped(step: PlanStep) -> None:
    """Mark a step as already satisfied by an earlier, partial run."""
    if isinstance(step, WorkflowStep):
        step.state = "skipped"
    elif isinstance(step, ConfirmStep):
        step.confirmed = True


def parse_plan(
    text: str,
    stack: list[StackEntry],
    *,
    pr_is_merged: Callable[[int], bool] = _pr_is_merged,
) -> list[PlanStep]:
    """Parse an edited plan back into steps. Raises ValueError if malformed.

    An ``l`` step may pin the PR it lands (``l 123`` or ``l <pr url>``) instead
    of taking the next PR in the stack positionally. A pinned PR that has
    already landed is no longer in the stack; such a step resolves to
    ``entry_index=-1`` and is skipped at execution time, so one plan file stays
    valid as the stack lands piece by piece. *pr_is_merged* checks that claim
    against GitHub and is injectable for testing.
    """
    steps: list[PlanStep] = []
    # The next stack entry an 'l' step may claim. Landing goes bottom-to-top,
    # so live land steps must take stack entries in order, starting at 0.
    next_index = 0
    land_steps = 0

    for line_num, raw_line in enumerate(text.splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if " #" in line:
            line = line[: line.index(" #")].strip()

        if line == "l" or line.startswith("l "):
            ref = line[2:].strip() if line.startswith("l ") else ""
            land = _resolve_land_step(
                ref,
                line_num,
                stack=stack,
                next_index=next_index,
                pr_is_merged=pr_is_merged,
            )
            steps.append(land)
            land_steps += 1
            # An already-landed step claims no stack entry, so the next live
            # 'l' still expects the PR currently at the bottom of the stack.
            if not land.already_landed:
                next_index += 1
        elif line.startswith("w "):
            workflow = line[2:].strip()
            if not workflow:
                raise ValueError(f"Line {line_num}: 'w' requires a workflow name")
            steps.append(WorkflowStep(workflow=workflow))
        elif line == "c" or line.startswith("c "):
            # The condition is optional: a bare 'c' just pauses to confirm.
            condition = line[2:].strip() if line.startswith("c ") else ""
            steps.append(ConfirmStep(condition=condition))
        else:
            raise ValueError(f"Line {line_num}: unrecognized step: {raw_line!r}")

    # A partial land is allowed (land only the bottom N PRs), but the plan must
    # land at least one PR. Because 'l' steps take stack entries bottom-to-top,
    # the landed PRs are always a prefix of the stack; the rest stay open.
    if land_steps == 0:
        raise ValueError("Plan has no 'l' steps — nothing to land")

    # Everything up to the last already-landed 'l' step happened in an earlier
    # run: the workflows ran and the confirmations were given before those PRs
    # could have merged. Mark them done so a re-run picks up where it left off
    # rather than re-prompting for sign-off on work that already shipped.
    # Note the `max(..., 0)`: with nothing landed there is no prefix to mark,
    # and a negative index here would slice from the *end* of the plan instead.
    for step in steps[: max(landed_prefix_end(steps), 0)]:
        _mark_skipped(step)

    return steps


def landed_prefix_end(plan: list[PlanStep]) -> int:
    """Index of the last ``l`` step whose PR had already landed, or -1 for none.

    Every step at or before this index is complete: the PR merged, and the
    workflow/confirm steps ahead of it must have run before it could. This is
    the boundary ``parse_plan`` marks done, the display calls "assumed
    completed", and ``execute_plan`` resumes from.
    """
    return max(
        (i for i, s in enumerate(plan) if isinstance(s, LandStep) and s.already_landed),
        default=-1,
    )


def _resolve_land_step(
    ref: str,
    line_num: int,
    *,
    stack: list[StackEntry],
    next_index: int,
    pr_is_merged: Callable[[int], bool],
) -> LandStep:
    """Resolve one ``l`` line against the stack. Raises ValueError if it can't."""
    if not ref:
        # Bare 'l': take the next PR in the stack, as plans always have.
        if next_index >= len(stack):
            raise ValueError(
                f"Line {line_num}: too many 'l' steps — only {len(stack)} PRs in stack"
            )
        return LandStep(entry_index=next_index, pr_number=stack[next_index].pr_number)

    pr_number = _parse_pr_ref(ref, line_num)
    index = next(
        (i for i, e in enumerate(stack) if e.pr_number == pr_number),
        None,
    )

    if index is None:
        # Not in the stack: the only benign explanation is that it already
        # landed and was rebased away. Confirm that with GitHub rather than
        # silently skipping a typo'd or unrelated PR number.
        if not pr_is_merged(pr_number):
            raise ValueError(
                f"Line {line_num}: PR #{pr_number} is not in the stack and has "
                "not been merged — check the PR number, or that you are landing "
                "the stack this plan was written for"
            )
        if next_index:
            # Already-landed steps are the completed prefix of a plan, so one
            # appearing after a step we still have to land means the stack
            # merged out of the order the plan describes.
            raise ValueError(
                f"Line {line_num}: PR #{pr_number} has already merged, but the "
                f"plan lands it after PR #{stack[next_index - 1].pr_number}, "
                "which is still open — the stack no longer matches this plan"
            )
        return LandStep(entry_index=-1, pr_number=pr_number)

    if index < next_index:
        # An earlier 'l' step already claimed this stack entry. Catching this
        # here also keeps `stack[next_index]` below in range: every remaining
        # case has next_index < index < len(stack).
        raise ValueError(
            f"Line {line_num}: PR #{pr_number} is already landed by an earlier "
            "'l' step — each PR can be landed only once"
        )

    if index != next_index:
        expected = stack[next_index].pr_number
        raise ValueError(
            f"Line {line_num}: plan lands PR #{pr_number} next, but the next PR "
            f"in the stack is #{expected} — the stack no longer matches this plan"
        )

    return LandStep(entry_index=index, pr_number=pr_number)


def plan_from_file(path: Path, stack: list[StackEntry]) -> list[PlanStep]:
    """Load a landing plan from a file.

    The file uses the exact same format as the interactive editor (see
    ``format_plan_for_editor``): ``l [pr]`` / ``w <workflow>`` /
    ``c [condition]`` steps, with ``#`` comments and blank lines ignored. It is
    parsed by the same ``parse_plan`` the editor uses, so a file saved from
    ``-i`` (or written by hand in that format) round-trips.
    """
    try:
        text = path.read_text()
    except OSError as e:
        console.print(f"[red]Could not read plan file {path}: {e}[/red]")
        sys.exit(1)
    try:
        return parse_plan(text, stack)
    except ValueError as e:
        console.print(f"[red]Invalid plan in {path}: {e}[/red]")
        sys.exit(1)


def edit_plan_interactive(
    stack: list[StackEntry],
    default_workflow: str | None = None,
    count: int | None = None,
) -> list[PlanStep]:
    """Open the default plan in $EDITOR and return the parsed result."""
    plan_text = format_plan_for_editor(
        stack, generate_default_plan(stack, default_workflow, count)
    )
    editor = os.environ.get("EDITOR", "vim")

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", prefix="autoland-plan-", delete=False
    ) as f:
        f.write(plan_text)
        plan_file = f.name

    try:
        console.print(f"[bold]Opening plan in {editor}...[/bold]")
        subprocess.run([editor, plan_file], check=True)
        edited_text = Path(plan_file).read_text()

        non_comment = [
            ln.strip()
            for ln in edited_text.splitlines()
            if ln.strip() and not ln.strip().startswith("#")
        ]
        if not non_comment:
            console.print("[yellow]Empty plan — aborting.[/yellow]")
            sys.exit(0)

        return parse_plan(edited_text, stack)
    except ValueError as e:
        console.print(f"[red]Invalid plan: {e}[/red]")
        sys.exit(1)
    except subprocess.CalledProcessError:
        console.print(f"[red]Editor ({editor}) exited with an error.[/red]")
        sys.exit(1)
    finally:
        Path(plan_file).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Status display
# ---------------------------------------------------------------------------

STATE_LABELS = {
    PRState.PENDING: "Pending",
    PRState.WAITING_FOR_APPROVAL: "Waiting for approval",
    PRState.WAITING_FOR_CHECKS: "Waiting for checks",
    PRState.IN_MERGE_QUEUE: "In merge queue",
    PRState.WAITING_FOR_WORKFLOW: "Waiting for workflow",
    PRState.MERGED: "Merged",
    PRState.FAILED: "Failed",
}

STATE_STYLES = {
    PRState.PENDING: "dim",
    PRState.WAITING_FOR_APPROVAL: "magenta",
    PRState.WAITING_FOR_CHECKS: "yellow",
    PRState.IN_MERGE_QUEUE: "cyan",
    PRState.WAITING_FOR_WORKFLOW: "blue",
    PRState.MERGED: "green",
    PRState.FAILED: "red bold",
}

_REVIEW_DECISION_DISPLAY = {
    "APPROVED": ("Approved", "green"),
    "REVIEW_REQUIRED": ("Review required", "magenta"),
    "CHANGES_REQUESTED": ("Changes requested", "red"),
}

# One glyph per broad outcome, so a plan scans as a column of icons before any
# of the words are read. All four are East Asian Wide (two cells), which keeps
# the columns after them aligned without measuring anything.
_ICON_DONE = "✅"
_ICON_ACTIVE = "⏳"
_ICON_PENDING = "⬜"
_ICON_FAILED = "❌"

_PR_STATE_ICONS = {
    PRState.PENDING: _ICON_PENDING,
    PRState.WAITING_FOR_APPROVAL: _ICON_ACTIVE,
    PRState.WAITING_FOR_CHECKS: _ICON_ACTIVE,
    PRState.IN_MERGE_QUEUE: _ICON_ACTIVE,
    PRState.WAITING_FOR_WORKFLOW: _ICON_ACTIVE,
    PRState.MERGED: _ICON_DONE,
    PRState.FAILED: _ICON_FAILED,
}

# "skipped" is set only by _mark_skipped, i.e. for a workflow that sits inside a
# plan's already-landed prefix — it must have run before those PRs could merge,
# so the plan assumes it did rather than claiming it was deliberately skipped.
_WORKFLOW_STEP_STATUS = {
    "pending": (_ICON_PENDING, "Pending", "dim"),
    "waiting": (_ICON_ACTIVE, "Waiting for workflow", "blue"),
    "succeeded": (_ICON_DONE, "Workflow complete", "green"),
    "failed": (_ICON_FAILED, "Failed", "red bold"),
    "skipped": (_ICON_DONE, "Assumed completed", "green"),
}

_STEP_KINDS = {LandStep: "Land", WorkflowStep: "Workflow", ConfirmStep: "Confirm"}

_DONE_ICONS = frozenset({_ICON_DONE})


def _land_entry(ctx: LandingContext, step: LandStep) -> StackEntry | None:
    """The stack entry a land step targets, or None if its PR already landed."""
    if step.already_landed:
        return None
    return ctx.stack[step.entry_index]


@dataclass(frozen=True)
class _StepRow:
    """One plan step, flattened into the fields the display lays out."""

    pointer: str  # "→" on the next step with work left, else " "
    icon: str
    number: str  # "1.", "2." ...
    status: str
    status_style: str
    kind: str  # Land / Workflow / Confirm
    key: str  # "#103", "deploy.yaml", the confirm condition
    note: str  # PR title, or "" when there is nothing to add
    detail: str  # second line: error message, retry counts, ...

    @property
    def done(self) -> bool:
        return self.icon in _DONE_ICONS

    @property
    def target(self) -> str:
        return f"{self.key} - {self.note}" if self.note else self.key


def _pointer_index(ctx: LandingContext, plan: list[PlanStep]) -> int:
    """Index of the step the "→" marks: the first one with work left to do.

    A plan replayed against a partially-landed stack opens with a prefix of
    steps that are already satisfied (see ``landed_prefix_end``). Pointing at
    step 1 there reads as "about to redo work that shipped last week", so the
    pointer starts past the prefix. Once a run is under way ``current_step`` has
    moved further along and wins.
    """
    return max(ctx.current_step, landed_prefix_end(plan) + 1)


def _step_status(
    step: PlanStep, ctx: LandingContext, *, index: int, prefix_end: int, pointer: int
) -> tuple[str, str, str]:
    """The (icon, label, style) a step shows in the status column."""
    if isinstance(step, LandStep):
        if step.already_landed:
            return _ICON_DONE, "Landed", "green"
        entry = ctx.stack[step.entry_index]
        return (
            _PR_STATE_ICONS.get(entry.state, _ICON_PENDING),
            STATE_LABELS.get(entry.state, str(entry.state)),
            STATE_STYLES.get(entry.state, ""),
        )
    if isinstance(step, WorkflowStep):
        return _WORKFLOW_STEP_STATUS.get(step.state, (_ICON_PENDING, "Pending", "dim"))
    # ConfirmStep. A confirmation inside the landed prefix was never actually
    # given in this run — it is inferred from the fact that later PRs merged —
    # so say so rather than claiming the user confirmed it.
    if step.confirmed:
        if index < prefix_end:
            return _ICON_DONE, "Assumed completed", "green"
        return _ICON_DONE, "Confirmed", "green"
    if index == pointer:
        return _ICON_ACTIVE, "Awaiting confirmation", "yellow"
    return _ICON_PENDING, "Pending", "dim"


def _step_target(step: PlanStep, ctx: LandingContext) -> tuple[str, str]:
    """The step's subject as a (key, note) pair — e.g. ("#103", "Fix the thing")."""
    if isinstance(step, LandStep):
        entry = _land_entry(ctx, step)
        if entry is None:
            return f"#{step.pr_number}", ""
        return f"#{entry.pr_number}", entry.title or "(untitled)"
    if isinstance(step, WorkflowStep):
        return step.workflow, ""
    return step.condition or "(no condition)", ""


def _step_detail(step: PlanStep, ctx: LandingContext) -> str:
    """The step's second line, or "" when the header says everything."""
    parts: list[str] = []
    if isinstance(step, LandStep):
        entry = _land_entry(ctx, step)
        if entry is None:
            return ""
        if entry.error_message:
            parts.append(entry.error_message)
        # Retry counts only matter once something has been retried.
        if entry.check_retries or entry.queue_retries:
            parts.append(f"retries CI {entry.check_retries} / MQ {entry.queue_retries}")
    elif isinstance(step, WorkflowStep) and step.error_message:
        parts.append(step.error_message)
    return " · ".join(parts)


def _plan_rows(ctx: LandingContext) -> list[_StepRow]:
    plan = ctx.plan or generate_default_plan(ctx.stack)
    pointer = _pointer_index(ctx, plan)
    prefix_end = landed_prefix_end(plan)
    rows = []
    for index, step in enumerate(plan):
        icon, status, style = _step_status(
            step, ctx, index=index, prefix_end=prefix_end, pointer=pointer
        )
        key, note = _step_target(step, ctx)
        rows.append(
            _StepRow(
                pointer="→" if index == pointer else " ",
                icon=icon,
                number=f"{index + 1}.",
                status=status,
                status_style=style,
                kind=_STEP_KINDS[type(step)],
                key=key,
                note=note,
                detail=_step_detail(step, ctx),
            )
        )
    return rows


def _plan_headline(rows: list[_StepRow]) -> str:
    done = sum(1 for r in rows if r.done)
    noun = "step" if len(rows) == 1 else "steps"
    parts = [f"Autoland plan · {len(rows)} {noun}"]
    if done:
        parts += [f"{done} done", f"{len(rows) - done} remaining"]
    return " · ".join(parts)


def _column_widths(rows: list[_StepRow]) -> tuple[int, int, int]:
    """Widths of the number, status, and kind columns (0s for an empty plan)."""
    return (
        max((len(r.number) for r in rows), default=0),
        max((len(r.status) for r in rows), default=0),
        max((len(r.kind) for r in rows), default=0),
    )


def _detail_indent(num_width: int) -> int:
    """Column the "↳" detail line starts at: under the status field."""
    return len("→ ") + len("⬜ ") + num_width + 1


def render_status_rich(ctx: LandingContext) -> Text:  # pragma: no cover
    """Render plan progress as compact, styled lines (rich-only path).

    Text is appended rather than parsed as markup, so a PR title containing
    something like "[1,25]" renders literally instead of being read as a style.
    """
    rows = _plan_rows(ctx)
    num_w, status_w, kind_w = _column_widths(rows)
    indent = " " * _detail_indent(num_w)

    # A long PR title would otherwise wrap back to column 0 and destroy the
    # alignment the format is built on. The plan is a scannable overview, so
    # trim the overflowing line instead — the full title is on the PR.
    out = Text(no_wrap=True, overflow="ellipsis")
    out.append(_plan_headline(rows), style="bold")
    out.append("\n")
    for row in rows:
        dim = "dim " if row.done else ""
        out.append("\n")
        out.append(f"{row.pointer} ", style="bold cyan")
        out.append(f"{row.icon} ")
        out.append(f"{row.number:<{num_w}} ", style=f"{dim}bold")
        out.append(f"{row.status:<{status_w}}", style=row.status_style)
        out.append(" | ", style="dim")
        out.append(f"{row.kind:<{kind_w}}", style=f"{dim}bold")
        out.append(" | ", style="dim")
        out.append(row.key, style=f"{dim}cyan")
        if row.note:
            out.append(f" - {row.note}", style=dim.strip())
        if row.detail:
            out.append(f"\n{indent}↳ {row.detail}", style="dim")
    if ctx.aborted:
        out.append(f"\n\nABORTED: {ctx.abort_reason}", style="red bold")
    return out


def render_status_plain(ctx: LandingContext) -> str:
    """Render plan progress as plain text (the no-rich fallback)."""
    rows = _plan_rows(ctx)
    num_w, status_w, kind_w = _column_widths(rows)
    indent = " " * _detail_indent(num_w)

    lines = ["", _plan_headline(rows), ""]
    for row in rows:
        lines.append(
            f"{row.pointer} {row.icon} {row.number:<{num_w}} "
            f"{row.status:<{status_w}} | {row.kind:<{kind_w}} | {row.target}"
        )
        if row.detail:
            lines.append(f"{indent}↳ {row.detail}")
    if ctx.aborted:
        lines += ["", f"ABORTED: {ctx.abort_reason}"]
    return "\n".join(lines)


def print_status(ctx: LandingContext) -> None:
    if HAVE_RICH:
        # no_wrap/overflow have to be given to print(): a Text's own settings
        # only apply when it is nested inside another renderable.
        console.print(render_status_rich(ctx), no_wrap=True, overflow="ellipsis")
    else:
        console.print(render_status_plain(ctx))


def _escape_markup(text: str) -> str:
    """Escape rich markup so dynamic text (e.g. a PR title like '[1,25]') is
    rendered literally instead of parsed as a style tag."""
    if HAVE_RICH:
        from rich.markup import escape  # noqa: PLC0415

        return escape(text)
    return text


def _describe_step(step: PlanStep, ctx: LandingContext) -> str:
    """A one-line, human-readable description of a plan step."""
    if isinstance(step, LandStep):
        entry = _land_entry(ctx, step)
        if entry is None:
            return f"Land PR #{step.pr_number} (already landed)"
        return f"Land PR #{entry.pr_number}: {entry.title or '(untitled)'}"
    if isinstance(step, WorkflowStep):
        return f"Wait for workflow {step.workflow}"
    # ConfirmStep
    if step.condition:
        return f"Manual confirmation: {step.condition}"
    return "Manual confirmation"


def _next_steps_lines(
    plan: list[PlanStep], from_index: int, ctx: LandingContext
) -> list[str]:
    """Numbered, markup-safe descriptions of the plan steps after `from_index`."""
    return [
        f"  {n}. {_escape_markup(_describe_step(s, ctx))}"
        for n, s in enumerate(plan[from_index + 1 :], 1)
    ]


# ---------------------------------------------------------------------------
# Landing logic
# ---------------------------------------------------------------------------


def _refresh_review(entry: StackEntry) -> None:
    """Update the entry's review decision, tolerating a transient gh failure."""
    with contextlib.suppress(RuntimeError):
        entry.review_decision = github.review_decision(entry.pr_number)


def wait_for_approval(
    entry: StackEntry, *, opts: AutolandOptions, ctx: LandingContext
) -> bool:
    """Wait until the PR has required approvals. Returns False if aborted."""
    pr_state = github.pr_state(entry.pr_number)
    if pr_state == "MERGED":
        entry.state = PRState.MERGED
        return True
    if pr_state == "CLOSED":
        entry.state = PRState.FAILED
        entry.error_message = "PR was closed"
        return False

    _refresh_review(entry)
    if entry.is_approved:
        return True

    entry.state = PRState.WAITING_FOR_APPROVAL
    label, _ = _REVIEW_DECISION_DISPLAY.get(entry.review_decision, ("not approved", ""))
    entry.error_message = label
    console.print(
        f"[magenta]PR #{entry.pr_number} is not yet approved "
        f"({entry.review_decision or 'REVIEW_REQUIRED'}). Waiting...[/magenta]"
    )

    while True:
        if ctx.aborted:
            return False
        console.print(
            f"[dim]PR #{entry.pr_number}: waiting for approval — "
            f"polling in {opts.poll_interval}s[/dim]"
        )
        resilient_sleep(opts.poll_interval)

        pr_state = github.pr_state(entry.pr_number)
        if pr_state == "MERGED":
            entry.state = PRState.MERGED
            return True
        if pr_state == "CLOSED":
            entry.state = PRState.FAILED
            entry.error_message = "PR was closed"
            return False

        _refresh_review(entry)
        if entry.is_approved:
            entry.error_message = ""
            console.print(f"[green]PR #{entry.pr_number} is now approved[/green]")
            return True
        if entry.review_decision == "CHANGES_REQUESTED":
            entry.error_message = "Changes requested — cannot proceed"
            console.print(
                f"[red]PR #{entry.pr_number} has changes requested. "
                "Resolve review comments and re-request review.[/red]"
            )


def wait_for_checks(
    entry: StackEntry, *, opts: AutolandOptions, ctx: LandingContext
) -> bool:
    """Wait for required checks to pass. Returns True on success."""
    entry.state = PRState.WAITING_FOR_CHECKS

    while True:
        if ctx.aborted:
            return False

        pr_state = github.pr_state(entry.pr_number)
        if pr_state == "MERGED":
            entry.state = PRState.MERGED
            return True
        if pr_state == "CLOSED":
            entry.state = PRState.FAILED
            entry.error_message = "PR was closed"
            return False

        result = evaluate_checks(github.checks(entry.pr_number), opts.required_checks)
        entry.error_message = result.summary

        if result.status == CheckStatus.ALL_PASSING:
            entry.error_message = "All checks passing"
            return True

        if result.status == CheckStatus.FAILED:
            if entry.check_retries >= opts.max_check_retries:
                entry.state = PRState.FAILED
                entry.error_message = (
                    f"Checks failed after {opts.max_check_retries} retries: "
                    f"{', '.join(result.failed_names)}"
                )
                return False
            entry.check_retries += 1
            console.print(
                f"[yellow]Rerunning failed checks "
                f"(attempt {entry.check_retries}/{opts.max_check_retries}): "
                f"{', '.join(result.failed_names)}[/yellow]"
            )
            github.rerun_failed(result.failed_runs)

        console.print(
            f"[dim]PR #{entry.pr_number}: {result.summary} — "
            f"polling in {opts.poll_interval}s[/dim]"
        )
        resilient_sleep(opts.poll_interval)


_MERGEABLE_STATES = {"CLEAN", "UNSTABLE", "HAS_HOOKS"}


@dataclass
class MergeableResult:
    ready: bool = False
    already_merged: bool = False
    error: str = ""


def wait_for_mergeable(
    entry: StackEntry, *, opts: AutolandOptions, ctx: LandingContext
) -> MergeableResult:
    """Wait until GitHub reports the PR as mergeable."""
    while True:
        if ctx.aborted:
            return MergeableResult(error="aborted")

        data = github.merge_state(entry.pr_number)
        pr_state = data.get("state", "")
        merge_state = data.get("mergeStateStatus", "UNKNOWN")
        mergeable = data.get("mergeable", "UNKNOWN")

        if pr_state == "MERGED":
            console.print(f"[green]PR #{entry.pr_number} is already merged[/green]")
            return MergeableResult(ready=True, already_merged=True)
        if pr_state == "CLOSED":
            entry.state = PRState.FAILED
            entry.error_message = "PR was closed"
            return MergeableResult(error="PR was closed")

        if merge_state in _MERGEABLE_STATES:
            console.print(
                f"[green]PR #{entry.pr_number} is mergeable "
                f"(mergeStateStatus={merge_state})[/green]"
            )
            return MergeableResult(ready=True)

        if mergeable == "CONFLICTING":
            entry.error_message = "PR has merge conflicts — waiting for resolution"
            console.print(
                f"\n[bold red]PR #{entry.pr_number} has merge conflicts! "
                "Resolve them on the PR branch and push; autoland will "
                "resume automatically.[/bold red]"
            )
            resilient_sleep(opts.poll_interval)
            continue

        # UNKNOWN can also mean "already in the merge queue".
        if merge_state == "UNKNOWN" and github.in_merge_queue(entry.pr_number):
            console.print(
                f"[cyan]PR #{entry.pr_number} is already in the merge queue — "
                "skipping enqueue[/cyan]"
            )
            return MergeableResult(ready=True, already_merged=False)

        entry.error_message = f"Waiting for mergeable state (currently {merge_state})"
        console.print(
            f"[dim]PR #{entry.pr_number}: mergeStateStatus={merge_state} — "
            f"polling in {opts.poll_interval}s[/dim]"
        )
        resilient_sleep(opts.poll_interval)


def _rewait_after_retry(
    entry: StackEntry, *, opts: AutolandOptions, ctx: LandingContext
) -> bool:
    """Re-verify approval and checks before a queue retry."""
    if not wait_for_approval(entry, opts=opts, ctx=ctx):
        return False
    entry.check_retries = 0
    return wait_for_checks(entry, opts=opts, ctx=ctx)


def enqueue_and_wait(
    entry: StackEntry, *, opts: AutolandOptions, ctx: LandingContext
) -> bool:
    """Add the PR to the merge queue and wait for it to merge."""
    while True:
        if ctx.aborted:
            return False

        mergeable_result = wait_for_mergeable(entry, opts=opts, ctx=ctx)
        if not mergeable_result.ready:
            return False
        if mergeable_result.already_merged:
            entry.state = PRState.MERGED
            entry.error_message = ""
            console.print(
                f"\n[bold green]PR #{entry.pr_number} already merged![/bold green]"
            )
            return True

        entry.state = PRState.IN_MERGE_QUEUE
        entry.error_message = "Adding to merge queue..."
        console.print(
            f"\n[bold cyan]Adding PR #{entry.pr_number} to merge queue[/bold cyan]"
        )

        try:
            github.enqueue(entry.pr_number)
        except RuntimeError as e:
            entry.error_message = f"Failed to enqueue: {e}"
            console.print(f"[red]Failed to add to merge queue: {e}[/red]")
            if entry.queue_retries >= opts.max_queue_retries:
                entry.state = PRState.FAILED
                entry.error_message = (
                    f"Failed to enqueue after {opts.max_queue_retries} attempts"
                )
                return False
            entry.queue_retries += 1
            if not _rewait_after_retry(entry, opts=opts, ctx=ctx):
                return False
            continue

        entry.error_message = "Waiting in merge queue..."
        awake_elapsed = 0.0
        while True:
            if ctx.aborted:
                return False
            if awake_elapsed > opts.merge_timeout:
                entry.state = PRState.FAILED
                entry.error_message = "Timed out waiting for merge queue"
                return False

            poll = github.poll_merge(entry.pr_number)
            if poll.merged:
                entry.state = PRState.MERGED
                entry.error_message = ""
                console.print(
                    f"\n[bold green]PR #{entry.pr_number} merged![/bold green]"
                )
                return True
            if poll.error:
                entry.state = PRState.FAILED
                entry.error_message = poll.error
                return False
            if poll.booted:
                console.print(
                    f"\n[yellow]PR #{entry.pr_number} was booted from the "
                    "merge queue[/yellow]"
                )
                if entry.queue_retries >= opts.max_queue_retries:
                    entry.state = PRState.FAILED
                    entry.error_message = (
                        f"Booted from queue {opts.max_queue_retries} times, giving up"
                    )
                    return False
                entry.queue_retries += 1
                if not _rewait_after_retry(entry, opts=opts, ctx=ctx):
                    return False
                break  # re-enqueue in outer loop

            mins = int(awake_elapsed) // 60
            entry.error_message = f"In merge queue ({mins}m elapsed)..."
            console.print(
                f"[dim]PR #{entry.pr_number}: in merge queue ({mins}m) — "
                f"polling in {opts.poll_interval}s[/dim]"
            )
            resilient_sleep(opts.poll_interval)
            awake_elapsed += opts.poll_interval


def execute_plan(
    ctx: LandingContext,
    common: cli.CommonArgs,
    opts: AutolandOptions,
    checkpointer: AutolandCheckpointer,
) -> bool:
    """Execute the landing plan from ctx.current_step. Returns True on success."""
    # A workflow checkpoint after the already-landed prefix of a plan should
    # wait for the code of the last PR that prefix landed. Only that last one
    # matters, so don't pay for a fetch + lookup on each of the others.
    last_prelanded = landed_prefix_end(ctx.plan)

    for step_idx in range(ctx.current_step, len(ctx.plan)):
        step = ctx.plan[step_idx]
        ctx.current_step = step_idx
        checkpointer.save(ctx)

        if isinstance(step, LandStep) and step.already_landed:
            console.print(
                f"\n[green]PR #{step.pr_number} already landed, skipping[/green]"
            )
            if step_idx == last_prelanded:
                _refresh_last_landed_sha(ctx, common, step.pr_number)

        elif isinstance(step, LandStep):
            entry = ctx.stack[step.entry_index]
            ctx.current_index = step.entry_index

            if entry.state == PRState.MERGED:
                console.print(
                    f"\n[green]PR #{entry.pr_number} already merged, skipping[/green]"
                )
                _refresh_last_landed_sha(ctx, common, entry.pr_number)
                continue

            console.print(
                f"\n{'=' * 60}\n[bold]Step {step_idx + 1}/{len(ctx.plan)}: "
                f"Landing PR #{entry.pr_number} — {entry.title}[/bold]\n{'=' * 60}"
            )

            if not wait_for_approval(entry, opts=opts, ctx=ctx):
                return _abort(
                    ctx,
                    checkpointer,
                    f"PR #{entry.pr_number} approval wait was aborted",
                )

            if entry.state == PRState.MERGED:
                _refresh_last_landed_sha(ctx, common, entry.pr_number)
            else:
                if not wait_for_checks(entry, opts=opts, ctx=ctx):
                    return _abort(
                        ctx,
                        checkpointer,
                        f"PR #{entry.pr_number} checks failed after retries",
                    )

                if entry.state == PRState.MERGED:
                    _refresh_last_landed_sha(ctx, common, entry.pr_number)
                else:
                    if not enqueue_and_wait(entry, opts=opts, ctx=ctx):
                        return _abort(
                            ctx,
                            checkpointer,
                            f"PR #{entry.pr_number} failed to merge",
                        )
                    _refresh_last_landed_sha(ctx, common, entry.pr_number)

            # Rebase + resubmit whenever commits remain above the one we just
            # landed — not only when more *land steps* follow. On a partial
            # land, the PRs we're leaving open still need their bases rebased
            # onto the newly-landed commit.
            has_commits_above = step.entry_index < len(ctx.stack) - 1
            if has_commits_above:
                try:
                    rebase_and_resubmit(common)
                except Exception as e:  # noqa: BLE001 - report any resubmit failure
                    return _abort(
                        ctx,
                        checkpointer,
                        f"Rebase failed after merging #{entry.pr_number}: {e}",
                    )

        elif isinstance(step, WorkflowStep):
            if step.state == "skipped":
                continue
            console.print(
                f"\n{'=' * 60}\n[bold]Step {step_idx + 1}/{len(ctx.plan)}: "
                f"Workflow checkpoint — {step.workflow}[/bold]\n{'=' * 60}"
            )
            if not ctx.last_landed_sha:
                _refresh_last_landed_sha(ctx, common)
            if not wait_for_workflow(step, opts=opts, common=common, ctx=ctx):
                return _abort(
                    ctx,
                    checkpointer,
                    f"Workflow {step.workflow} failed or timed out",
                )

        elif isinstance(step, ConfirmStep):
            if step.confirmed:
                continue
            question = (
                f'Confirm "{_escape_markup(step.condition)}" is complete — '
                "ready to proceed?"
                if step.condition
                else "Ready to proceed?"
            )
            lines = [
                f"\n{'=' * 60}",
                (
                    f"[bold yellow]Step {step_idx + 1}/{len(ctx.plan)}: "
                    "Manual confirmation required[/bold yellow]"
                ),
                f"{'=' * 60}\n",
                f"[bold]{question}[/bold]\n",
            ]
            next_lines = _next_steps_lines(ctx.plan, step_idx, ctx)
            if next_lines:
                lines.append("[bold]Next steps:[/bold]")
                lines.extend(next_lines)
            else:
                lines.append("[dim]This is the final step in the plan.[/dim]")
            console.print("\n".join(lines))
            while True:
                try:
                    answer = console.input(
                        "[yellow]Type y/Y then Enter to continue "
                        "(Ctrl+C to abort): [/yellow]"
                    ).strip()
                except EOFError:
                    return _abort(
                        ctx,
                        checkpointer,
                        "Confirm step received EOF — cannot confirm in "
                        "non-interactive mode",
                    )
                if answer in ("y", "Y"):
                    break
                console.print("[dim]Type 'y' or 'Y' to confirm.[/dim]")
            step.confirmed = True
            console.print("[green]Confirmed[/green]")

    ctx.current_step = len(ctx.plan)
    checkpointer.save(ctx)
    return True


def _abort(
    ctx: LandingContext, checkpointer: AutolandCheckpointer, reason: str
) -> bool:
    ctx.abort_reason = reason
    ctx.aborted = True
    checkpointer.save(ctx)
    return False


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------


def register_parser(
    subparsers: argparse._SubParsersAction, common_parser: argparse.ArgumentParser
) -> None:
    """Register the `autoland` subparser. Called from cli.create_argparser."""
    p = subparsers.add_parser(
        "autoland",
        help="Land the whole stack through the GitHub merge queue",
        parents=[common_parser],
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Discover and display the stack, then exit.",
    )
    p.add_argument(
        "--max-check-retries",
        type=int,
        default=None,
        help="Max times to rerun failed CI checks (config: autoland.max_check_retries).",
    )
    p.add_argument(
        "--max-queue-retries",
        type=int,
        default=None,
        help="Max retries after a merge-queue boot (config: autoland.max_queue_retries).",
    )
    p.add_argument(
        "--poll-interval",
        type=int,
        default=None,
        help="Seconds between status polls (config: autoland.poll_interval).",
    )
    p.add_argument(
        "--workflow-timeout",
        type=int,
        default=None,
        help=(
            "Seconds to wait for a workflow checkpoint "
            "(config: autoland.workflow_timeout)."
        ),
    )
    p.add_argument(
        "-n",
        "--count",
        type=int,
        default=None,
        metavar="N",
        help="Land only the bottom N PRs of the stack (default: the whole stack).",
    )
    p.add_argument(
        "--branch",
        default=None,
        metavar="BRANCH",
        help="Land a stack rooted on BRANCH using a temporary worktree.",
    )
    p.add_argument(
        "--always-cleanup",
        action="store_true",
        help="Always remove the temporary worktree, even on failure.",
    )
    # The plan comes from one source: the default, the interactive editor, or a
    # file. -i and --plan-file are therefore mutually exclusive.
    plan_source = p.add_mutually_exclusive_group()
    plan_source.add_argument(
        "-i",
        "--interactive",
        action="store_true",
        help="Edit the landing plan in $EDITOR (add workflow/confirm checkpoints).",
    )
    plan_source.add_argument(
        "--plan-file",
        type=Path,
        default=None,
        metavar="PATH",
        help=(
            "Load the landing plan from a file (same format as the -i editor: "
            "'l' / 'w <workflow>' / 'c [condition]' lines)."
        ),
    )
    p.add_argument(
        "--resume",
        action="store_true",
        help="Resume a previously interrupted run from its checkpoint.",
    )
    p.add_argument(
        "--state-file",
        type=Path,
        default=None,
        metavar="PATH",
        help="Override the state file path (default: ~/.stack-pr/autoland/<branch>.json).",
    )


def run_autoland(
    common: cli.CommonArgs,
    args: argparse.Namespace,
    config: configparser.ConfigParser,
) -> None:
    """Entry point for `stack-pr autoland`."""
    opts = AutolandOptions.from_config_and_args(config, args)

    # Merge-queue is the only supported strategy for now. Fail early otherwise.
    if not opts.merge_queue:
        raise NotImplementedError(
            "stack-pr autoland currently supports only repositories that use "
            "the GitHub merge queue. Enable it with:\n"
            "    stack-pr config autoland.merge_queue=true"
        )

    # A file plan replaces the whole plan, so --count (which only shapes the
    # generated default) and --resume (which restores the plan from a
    # checkpoint) have nothing to act on.
    if opts.plan_file is not None and opts.count is not None:
        console.print(
            "[red]--plan-file and --count can't be combined: the file already "
            "specifies which PRs to land.[/red]"
        )
        sys.exit(1)
    if opts.plan_file is not None and opts.resume:
        console.print(
            "[red]--plan-file and --resume can't be combined: a resumed run "
            "restores its plan from the checkpoint.[/red]"
        )
        sys.exit(1)

    if opts.resume:
        _run_resume(common, opts)
        return
    _run_fresh(common, opts)


def _dispose_worktree(
    worktree: Worktree | None, opts: AutolandOptions, *, succeeded: bool
) -> None:
    """Remove the worktree, or preserve it after a failure for debugging."""
    if worktree is None:
        return
    if succeeded or opts.always_cleanup:
        worktree.remove()
    else:
        worktree.announce_preserved()


def _install_signal_handler(
    ctx: LandingContext,
    checkpointer: AutolandCheckpointer,
    worktree: Worktree | None,
    opts: AutolandOptions,
) -> None:
    def handler(_sig: int, _frame: object) -> None:
        ctx.aborted = True
        ctx.abort_reason = "User interrupted (Ctrl+C)"
        checkpointer.save(ctx)
        console.print(
            "\n[red bold]Interrupted! State saved. Resume with --resume.[/red bold]\n"
        )
        print_status(ctx)
        _dispose_worktree(worktree, opts, succeeded=False)
        sys.exit(130)

    signal.signal(signal.SIGINT, handler)


def _finish(
    ctx: LandingContext,
    checkpointer: AutolandCheckpointer,
    worktree: Worktree | None,
    opts: AutolandOptions,
    *,
    success: bool,
) -> None:
    console.print("\n")
    print_status(ctx)
    if success:
        # Count only the steps this run actually landed: a step whose PR landed
        # in an earlier run has no entry in today's stack to count against.
        landed = sum(
            1 for s in ctx.plan if isinstance(s, LandStep) and not s.already_landed
        )
        total = len(ctx.stack)
        if landed < total:
            console.print(
                f"\n[bold green]Landed {landed} of {total} PRs; "
                f"{total - landed} still open in the stack.[/bold green]\n"
            )
        else:
            console.print("\n[bold green]All PRs landed successfully![/bold green]\n")
        checkpointer.delete()
    else:
        console.print(f"\n[bold red]Landing failed: {ctx.abort_reason}[/bold red]\n")
        console.print(
            f"[dim]State saved to {checkpointer.path} — resume with --resume[/dim]\n"
        )
    _dispose_worktree(worktree, opts, succeeded=success)
    if not success:
        sys.exit(1)


def _confirm_overwrite_state(state_path: Path) -> bool:
    """Warn about an existing checkpoint and confirm overwriting it."""
    console.print(
        "\n[bold yellow]An autoland is already in progress for this "
        "branch.[/bold yellow]\n"
        f"[yellow]A checkpoint from that run exists at {state_path}.[/yellow]\n"
        "[yellow]Starting a new autoland overwrites it — you will no longer be "
        "able to resume the previous run.[/yellow]\n"
        "[dim]To continue the previous run instead, re-run with --resume.[/dim]\n"
    )
    try:
        answer = console.input(
            "[yellow]Overwrite and start a new autoland? Type y/Y to confirm "
            "(anything else aborts): [/yellow]"
        ).strip()
    except EOFError:
        return False
    return answer in ("y", "Y")


def _run_fresh(common: cli.CommonArgs, opts: AutolandOptions) -> None:
    branch = opts.branch or _current_branch()
    state_path = opts.state_file or AutolandCheckpointer.default_path(branch)

    # A dry run only previews the plan; it neither writes state nor competes for
    # the lock, so let it run freely alongside a real autoland.
    lock: AutolandLock | None = None
    if not opts.dry_run:
        lock = AutolandLock.for_state(state_path)
        if not lock.acquire():
            console.print(
                f"[red]An autoland is already running for branch "
                f"[bold]{branch}[/bold]. Wait for it to finish before starting "
                "another.[/red]"
            )
            sys.exit(1)

    try:
        # An existing state file means a previous run was interrupted and can be
        # resumed; starting fresh would clobber it, so confirm first.
        if (
            lock is not None
            and state_path.exists()
            and not _confirm_overwrite_state(state_path)
        ):
            console.print("[red]Aborted — the previous autoland is untouched.[/red]")
            return

        worktree: Worktree | None = None
        if opts.branch:
            worktree = Worktree(opts.branch)
            worktree.create()
            console.print(
                f"[green]Working in temporary worktree for [bold]{opts.branch}"
                "[/bold][/green]\n"
            )

        # Deduce the base now that any worktree has been created and we've
        # switched into it. With --branch, HEAD in the primary checkout points
        # at a different branch, so deducing earlier would freeze a base that
        # isn't an ancestor of the stack. deduce_base honors an explicit --base.
        common = cli.deduce_base(common)

        console.print("\n[bold]Discovering stack...[/bold]\n")
        stack = discover_stack(common)
        if not stack:
            console.print("[red]No stack found on the current branch.[/red]")
            sys.exit(1)
        enrich_stack(stack)

        if opts.count is not None and not 1 <= opts.count <= len(stack):
            console.print(
                f"[red]--count must be between 1 and {len(stack)} "
                f"(the stack has {len(stack)} PRs).[/red]"
            )
            sys.exit(1)

        if opts.plan_file is not None:
            plan = plan_from_file(opts.plan_file, stack)
        elif opts.interactive:
            plan = edit_plan_interactive(stack, opts.default_workflow, opts.count)
        else:
            plan = generate_default_plan(stack, count=opts.count)
        ctx = LandingContext(stack=stack, plan=plan)

        checkpointer = AutolandCheckpointer(
            path=state_path, branch=branch, base=common.target
        )

        print_status(ctx)
        if opts.dry_run:
            console.print("\n[yellow]Dry run — exiting.[/yellow]")
            return

        console.print(f"[dim]State file: {checkpointer.path}[/dim]\n")
        _install_signal_handler(ctx, checkpointer, worktree, opts)
        _finish(
            ctx,
            checkpointer,
            worktree,
            opts,
            success=execute_plan(ctx, common, opts, checkpointer),
        )
    finally:
        if lock is not None:
            lock.release()


def _run_resume(common: cli.CommonArgs, opts: AutolandOptions) -> None:
    if opts.state_file:
        sf_path = opts.state_file
    elif opts.branch:
        sf_path = AutolandCheckpointer.default_path(opts.branch)
    else:
        sf_path = AutolandCheckpointer.default_path(_current_branch())

    if not sf_path.exists():
        console.print(f"[red]No state file found at {sf_path}[/red]")
        sys.exit(1)

    lock = AutolandLock.for_state(sf_path)
    if not lock.acquire():
        console.print(
            "[red]An autoland is already running for this branch. Wait for it "
            "to finish before resuming.[/red]"
        )
        sys.exit(1)

    try:
        console.print(
            f"[bold]Resuming from checkpoint: [cyan]{sf_path}[/cyan][/bold]\n"
        )
        try:
            checkpointer, ctx = AutolandCheckpointer.load(sf_path)
        except (ValueError, json.JSONDecodeError, KeyError) as e:
            console.print(f"[red]Failed to load state file: {e}[/red]")
            sys.exit(1)

        if opts.branch and opts.branch != checkpointer.branch:
            console.print(
                f"[red]--branch {opts.branch} does not match saved branch "
                f"{checkpointer.branch}[/red]"
            )
            sys.exit(1)

        worktree: Worktree | None = None
        if opts.branch or checkpointer.branch != _current_branch():
            worktree = Worktree(opts.branch or checkpointer.branch)
            worktree.create()

        # Deduce the base against the (possibly worktree) HEAD, for the same
        # reason as in _run_fresh: the primary checkout's HEAD may be a
        # different branch than the one being landed.
        common = cli.deduce_base(common)

        console.print("[dim]Refreshing PR state from GitHub...[/dim]")
        enrich_stack(ctx.stack)
        ctx.aborted = False
        ctx.abort_reason = ""

        if ctx.current_step >= len(ctx.plan):
            console.print(
                "[green]All steps already completed — nothing to resume.[/green]"
            )
            checkpointer.delete()
            _dispose_worktree(worktree, opts, succeeded=True)
            return

        print_status(ctx)
        console.print(f"[dim]State file: {sf_path}[/dim]\n")
        _install_signal_handler(ctx, checkpointer, worktree, opts)
        _finish(
            ctx,
            checkpointer,
            worktree,
            opts,
            success=execute_plan(ctx, common, opts, checkpointer),
        )
    finally:
        lock.release()
