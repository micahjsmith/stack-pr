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
from dataclasses import asdict, astuple, dataclass, field, replace
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Union

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
# plain-text console can strip them. Only the style words this module actually
# writes are recognized: a looser pattern also eats bracketed text that came
# from GitHub — a PR titled "[wip] make it fast" would silently lose its tag.
_STYLE_WORDS = (
    "black|blue|bold|cyan|dim|green|italic|magenta|red|white|yellow|underline"
)
_RE_MARKUP = re.compile(rf"\[/?(?:{_STYLE_WORDS})(?: (?:{_STYLE_WORDS}))*\]")


class _PlainConsole:
    """Minimal stand-in for rich.Console that strips markup."""

    def print(self, *args: object, **_kwargs: object) -> None:
        print(*[_RE_MARKUP.sub("", str(a)) for a in args])

    def input(self, prompt: object = "") -> str:
        return input(_RE_MARKUP.sub("", str(prompt)))


try:
    from rich.console import Console, Group
    from rich.padding import Padding
    from rich.text import Text

    HAVE_RICH = True
except ImportError:  # pragma: no cover - exercised only without the extra
    Console = _PlainConsole  # type: ignore[assignment,misc]
    Group = None  # type: ignore[assignment,misc]
    Padding = None  # type: ignore[assignment,misc]
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
    merge_as_stack: bool = True
    replan: bool = False

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
        merge_as_stack = getattr(args, "merge_as_stack", None)
        if merge_as_stack is None:
            merge_as_stack = config.getboolean(
                "autoland", "merge_as_stack", fallback=True
            )
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
            merge_as_stack=merge_as_stack,
            replan=bool(getattr(args, "replan", False)),
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
        # GitHub reports no review decision at all when the target branch
        # requires no review, and then there is no approval to wait for.
        return self.review_decision in ("APPROVED", "")


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
    # The plan file the run was started from, so `--replan` can re-read it.
    plan_file: Path | None = None

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
            "plan_file": str(self.plan_file) if self.plan_file else None,
            "current_step": ctx.current_step,
            "last_landed_sha": ctx.last_landed_sha,
            # Kept so `autoland --status` can say why a stopped run stopped.
            "abort_reason": ctx.abort_reason,
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
            aborted=bool(data.get("abort_reason")),
            abort_reason=data.get("abort_reason", ""),
            last_landed_sha=data.get("last_landed_sha", ""),
        )
        plan_file = data.get("plan_file")
        return cls(
            path=path,
            branch=data["branch"],
            base=data["base"],
            plan_file=Path(plan_file) if plan_file else None,
        ), ctx


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
        while True:
            fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o644)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                os.close(fd)
                return False
            # The previous holder unlinks the file as it releases, so the flock
            # may have landed on a file that no longer has this name — a lock
            # nobody else can see. Only a lock on the file at the path counts.
            if self._names_locked_file(fd):
                break
            os.close(fd)
        self._fd = fd
        # Record the holder so `autoland --status` can name the running process.
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        return True

    def _names_locked_file(self, fd: int) -> bool:
        try:
            at_path, locked = self.path.stat(), os.fstat(fd)
        except FileNotFoundError:
            return False
        return (at_path.st_dev, at_path.st_ino) == (locked.st_dev, locked.st_ino)

    def is_held(self) -> bool:
        """Whether some process holds the lock right now.

        Probes without creating or removing the lock file, so it is safe to call
        while a run is live. The probe holds a shared lock for an instant; a run
        starting in exactly that instant would see the branch as busy.
        """
        try:
            fd = os.open(self.path, os.O_RDONLY)
        except FileNotFoundError:
            return False
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except OSError:
            return True
        finally:
            # Closing the descriptor also drops the probe's own lock.
            os.close(fd)
        return False

    def holder_pid(self) -> int | None:
        """The PID the holder recorded, or None if unknown."""
        try:
            return int(self.path.read_text().strip())
        except (OSError, ValueError):
            return None

    def release(self) -> None:
        """Release the lock and remove the lock file (no-op if not held)."""
        if self._fd is None:
            return
        # Unlink while still holding the flock: a waiter that takes it next
        # then finds the name gone or reused, and retries (see acquire).
        # Unlocking first would let it lock a file about to lose its name.
        try:
            self.path.unlink(missing_ok=True)
        finally:
            os.close(self._fd)  # closing the descriptor releases the flock
            self._fd = None


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

_MERGE_QUEUE_QUERY = """
query($owner: String!, $repo: String!, $branch: String!) {
  repository(owner: $owner, name: $repo) {
    mergeQueue(branch: $branch) { id }
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

    # GitHub's native stacked PRs. A stack is an explicit server-side object —
    # GitHub does not infer one from a chain of PR bases — and once a PR is in
    # one, only the asynchronous merge API can merge it. See
    # https://docs.github.com/en/rest/pulls/stacks and
    # https://docs.github.com/en/rest/pulls/pulls#merge-a-pull-request-asynchronously

    def _api(self, method: str, path: str, body: dict | None = None) -> Any:  # noqa: ANN401
        owner, repo = self.owner_repo()
        cmd = ["gh", "api", "-X", method, f"repos/{owner}/{repo}/{path}"]
        if body is not None:
            cmd += ["--input", "-"]
        result = run(
            cmd,
            quiet=True,
            input_data=json.dumps(body).encode() if body is not None else None,
        )
        return json.loads(result.stdout) if result.stdout.strip() else None

    def find_native_stack(self, pr_number: int) -> dict | None:
        """The GitHub stack *pr_number* belongs to, or ``None`` if it has none."""
        stacks = self._api("GET", f"stacks?pull_request={pr_number}")
        return stacks[0] if isinstance(stacks, list) and stacks else None

    def create_native_stack(self, pr_numbers: list[int]) -> dict:
        """Register *pr_numbers* (bottom first) as a GitHub stack."""
        stack = self._api("POST", "stacks", {"pull_requests": pr_numbers})
        assert isinstance(stack, dict)
        return stack

    def unstack_native_stack(self, native_stack_number: int) -> None:
        """Remove a stack's unmerged PRs from it; queued PRs stay queued."""
        self._api("POST", f"stacks/{native_stack_number}/unstack")

    def set_base(self, pr_number: int, base: str) -> None:
        run(["gh", "pr", "edit", str(pr_number), "--base", base], quiet=True)

    def has_merge_queue(self, branch: str) -> bool | None:
        """Whether *branch* has a merge queue, or ``None`` if GitHub can't say."""
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
                    f"branch={branch}",
                    "-f",
                    f"query={_MERGE_QUEUE_QUERY}",
                ],
                quiet=True,
            )
            repository = json.loads(result.stdout)["data"]["repository"]
        except (RuntimeError, json.JSONDecodeError, KeyError, TypeError):
            return None
        return repository.get("mergeQueue") is not None

    def merge_async(self, pr_number: int, *, merge_queue: bool) -> str | None:
        """Request a merge of *pr_number* and every open PR below it in its stack.

        Returns the request's id for ``merge_async_status``, or ``None`` when
        GitHub didn't return one (the PR was already merged or queued).
        """
        # The merge queue merges with its own configured method, and GitHub
        # rejects a request that names one ("Custom merge params are not
        # supported when merging via a merge queue").
        body = (
            {"merge_action": "merge_queue"}
            if merge_queue
            else {"merge_action": "direct_merge", "merge_method": "squash"}
        )
        data = self._api("PUT", f"pulls/{pr_number}/merge-async", body)
        if not isinstance(data, dict):
            return None
        details = data.get("details")
        uuid = details.get("uuid") if isinstance(details, dict) else None
        return uuid or data.get("uuid") or None

    def merge_async_status(self, pr_number: int, uuid: str) -> tuple[str, str]:
        """``(status, message)`` of a merge request.

        Status is one of ``pending``, ``enqueued``, ``merged``, ``failed``.
        """
        data = self._api("GET", f"pulls/{pr_number}/merge-async/{uuid}")
        if not isinstance(data, dict):
            return "", ""
        details = data.get("details")
        message = details.get("message", "") if isinstance(details, dict) else ""
        return data.get("status", ""), message or ""

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

    def contains(self, ancestor: str, descendant: str) -> bool | None:
        """Whether ``ancestor`` is an ancestor of ``descendant``, per GitHub.

        Asks the compare API for the merge base of the two commits, so this
        works for commits that were never fetched into the local clone.
        Returns ``None`` if GitHub could not answer (network error, or a commit
        it does not know either) — that is "unknown", not "no".
        """
        try:
            owner, repo = self.owner_repo()
            # per_page=1 trims the (unused) commit list in the response.
            path = f"repos/{owner}/{repo}/compare/{ancestor}...{descendant}"
            result = run(
                ["gh", "api", f"{path}?per_page=1", "--jq", ".merge_base_commit.sha"],
                quiet=True,
            )
        except RuntimeError:
            return None
        merge_base = result.stdout.strip()
        if not merge_base or merge_base == "null":  # jq prints null for a miss
            return None
        # ancestor is an ancestor of descendant exactly when it *is* the merge
        # base of the two (this also covers the identical-commit case).
        return _sha_eq(merge_base, ancestor)


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

    Once landing starts (``landing`` is set), the run's outcome decides
    whether the worktree is kept (see ``_dispose_worktree``). Before then
    nothing worth debugging has happened, so ``remove_unless_landing`` deletes
    it however the setup ended.
    """

    def __init__(self, branch: str) -> None:
        self.branch = branch
        self.path: Path | None = None
        self.landing = False
        self._orig_cwd: str | None = None

    def remove_unless_landing(self) -> None:
        if not self.landing:
            self.remove()

    def create(self) -> None:
        tmpdir = tempfile.mkdtemp(prefix="autoland-")
        worktree_dir = str(Path(tmpdir) / "repo")
        console.print(
            f"[bold]Creating temporary worktree for [cyan]{self.branch}[/cyan] "
            f"at {worktree_dir}[/bold]"
        )
        try:
            subprocess.run(
                ["git", "worktree", "add", "-f", worktree_dir, self.branch],
                check=True,
                capture_output=True,
                text=True,
            )
        except BaseException:
            shutil.rmtree(tmpdir, ignore_errors=True)
            raise
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
    return _stack_entries(
        cli.get_stack(base=common.base, head=common.head, verbose=common.verbose)
    )


def _stack_entries(raw: list[cli.StackEntry]) -> list[StackEntry]:
    entries: list[StackEntry] = []
    for e in raw:
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


# git's own minimum abbreviation length
_MIN_SHA_LEN = 7


def _sha_eq(a: str, b: str) -> bool:
    """Whether two (possibly abbreviated) SHAs name the same commit.

    Comparison is on the shorter SHA's length; anything shorter than
    ``_MIN_SHA_LEN`` is too ambiguous to call equal, so it is reported as
    different.
    """
    n = min(len(a), len(b))
    if n < _MIN_SHA_LEN:
        return False
    return a[:n].lower() == b[:n].lower()


def _local_is_ancestor(ancestor: str, descendant: str) -> bool | None:
    """Whether ``ancestor`` is an ancestor of ``descendant``, per this clone.

    ``None`` means the local repo cannot answer: ``git merge-base
    --is-ancestor`` exits 128 ("Not a valid commit name") when either commit is
    missing from this clone, rather than 1 for a genuine "no". The two must not
    be conflated — a workflow run's head commit is routinely absent here,
    because ``origin/<target>`` advances while we poll.
    """
    result = run(
        ["git", "merge-base", "--is-ancestor", ancestor, descendant],
        check=False,
        quiet=True,
    )
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    return None


class _Ancestry:
    """Answers "does this workflow run's commit include the code we landed?".

    Tries the local repo first, then a fetch of the target branch, then the
    GitHub compare API. The fetch is what usually resolves it: the run's head
    commit is a commit on ``<remote>/<target>`` that this clone has not seen
    yet. The API call covers the rest (e.g. a merge-queue commit that never
    landed on the target branch, or a clone that cannot fetch).

    Commits are immutable, so a definitive verdict is cached for the rest of
    the wait; the poll loop re-examines the same runs every interval.
    """

    def __init__(self, common: cli.CommonArgs) -> None:
        self._common = common
        self._verdicts: dict[tuple[str, str], bool] = {}
        self._fetched_for: set[str] = set()

    def contains(self, ancestor: str, descendant: str) -> bool | None:
        """Whether ``descendant`` is ``ancestor`` or a commit after it.

        ``None`` if neither git nor GitHub could tell us.
        """
        if _sha_eq(ancestor, descendant):
            return True
        key = (ancestor, descendant)
        if key in self._verdicts:
            return self._verdicts[key]

        verdict = _local_is_ancestor(ancestor, descendant)
        if verdict is None and descendant not in self._fetched_for:
            # A commit is missing locally. Fetch the target branch once for
            # this commit and retry; fetching again for the same commit would
            # not tell us anything new.
            self._fetched_for.add(descendant)
            self._fetch_target()
            verdict = _local_is_ancestor(ancestor, descendant)
        if verdict is None:
            verdict = github.contains(ancestor, descendant)

        if verdict is not None:
            self._verdicts[key] = verdict
        return verdict

    def _fetch_target(self) -> None:
        with contextlib.suppress(RuntimeError):  # fetch is non-critical
            run(
                ["git", "fetch", self._common.remote, self._common.target],
                quiet=True,
            )


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

    ancestry = _Ancestry(common)
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
            verdict = ancestry.contains(target_sha, run_sha)
            if verdict is None:
                console.print(
                    f"[yellow]Warning: could not tell whether run "
                    f"{run_sha[:12]} includes {target_sha[:12]}; "
                    "will retry[/yellow]"
                )
                continue
            if verdict:
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


# Where the comments in a generated plan start, and how far past a step that
# reaches into that column they move instead. Kept in sync with COMMENT_COLUMN
# and COMMENT_GAP in editors/vscode/src/format.js, which formats plans the same
# way, so a generated plan is already formatted.
PLAN_COMMENT_COLUMN = 30
PLAN_COMMENT_GAP = 4


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

    # Each step as (keyword, line, trailing comment), so the comments can be
    # aligned once the longest step is known. The steps are emitted as one
    # contiguous block, which is the unit comments are aligned within.
    steps: list[tuple[str, str, str]] = []
    for step in plan:
        if isinstance(step, LandStep):
            if step.already_landed:
                steps.append(("l", f"l {step.pr_number}", "already landed"))
                continue
            entry = stack[step.entry_index]
            steps.append(("l", f"l {entry.pr_number}", entry.title or ""))
        elif isinstance(step, WorkflowStep):
            steps.append(("w", f"w {step.workflow}", ""))
        elif isinstance(step, ConfirmStep):
            steps.append(("c", f"c {step.condition}".rstrip(), ""))

    # Only 'l' and 'w' steps decide the column: a confirm condition is free
    # text that routinely runs long, and letting it decide would drag every
    # comment off to the right.
    longest = max((len(line) for kw, line, _ in steps if kw in ("l", "w")), default=0)
    column = max(PLAN_COMMENT_COLUMN, longest + PLAN_COMMENT_GAP)
    lines.extend(
        f"{line}{' ' * max(column - len(line), 1)}# {comment}" if comment else line
        for _kw, line, comment in steps
    )

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

    # Everything before the last already-landed 'l' step happened in an earlier
    # run: the workflows ran and the confirmations were given before those PRs
    # could have merged. Mark them done so a re-run picks up where it left off
    # rather than re-prompting for sign-off on work that already shipped.
    for index, step in enumerate(steps):
        if is_assumed_completed(steps, index):
            _mark_skipped(step)

    return steps


def landed_prefix_end(plan: list[PlanStep]) -> int:
    """Index of the last ``l`` step whose PR had already landed, or -1 for none.

    Every step at or before this index is complete: the PR merged, and the
    workflow/confirm steps ahead of it must have run before it could. This is
    the boundary ``execute_plan`` resumes from and the display points at.

    Prefer ``is_assumed_completed`` for "is this step inside the prefix?" — the
    -1 makes that comparison work out on its own, but it slices from the *end*
    of a list if it is ever used as a bound directly.
    """
    return max(
        (i for i, s in enumerate(plan) if isinstance(s, LandStep) and s.already_landed),
        default=-1,
    )


def is_assumed_completed(plan: list[PlanStep], index: int) -> bool:
    """Whether the step at *index* sits inside the plan's already-landed prefix.

    Such a step was never run in this session; it is credited as done because
    the PRs after it could not have merged otherwise.
    """
    return index < landed_prefix_end(plan)


# ---------------------------------------------------------------------------
# Replanning: carry a previous run's progress over to a new plan
# ---------------------------------------------------------------------------

# A workflow or confirm step's identity for replanning: its kind, its text, and
# the PRs landed before it. A step means "once these PRs have landed, this
# holds", so a result recorded under the same PRs is still true in a new plan.
_CheckpointKey = tuple[str, str, frozenset[int]]


def _land_pr(step: LandStep, stack: list[StackEntry]) -> int | None:
    if step.pr_number is not None:
        return step.pr_number
    return None if step.already_landed else stack[step.entry_index].pr_number


def _checkpoint_keys(
    plan: list[PlanStep], stack: list[StackEntry]
) -> list[_CheckpointKey | None]:
    """Each step's ``_CheckpointKey``, or None for a land step."""
    landed: set[int] = set()
    keys: list[_CheckpointKey | None] = []
    for step in plan:
        if isinstance(step, LandStep):
            pr = _land_pr(step, stack)
            if pr is not None:
                landed.add(pr)
            keys.append(None)
        elif isinstance(step, WorkflowStep):
            keys.append(("w", step.workflow, frozenset(landed)))
        else:
            keys.append(("c", step.condition.strip(), frozenset(landed)))
    return keys


def _checkpoint_done(step: PlanStep) -> bool:
    if isinstance(step, WorkflowStep):
        return step.state in ("succeeded", "skipped")
    return isinstance(step, ConfirmStep) and step.confirmed


def _credit(new: PlanStep, old: PlanStep) -> None:
    """Record on *new* the result *old* reached in the previous run."""
    if isinstance(new, WorkflowStep) and isinstance(old, WorkflowStep):
        new.state = old.state
        new.error_message = ""
    elif isinstance(new, ConfirmStep):
        new.confirmed = True


def carry_over_progress(
    old: LandingContext, plan: list[PlanStep], stack: list[StackEntry]
) -> list[int]:
    """Credit *plan*'s workflow and confirm steps with results from *old*.

    *plan* is a freshly parsed plan for the current *stack*. Land steps need no
    help: whether a PR merged is GitHub's to say, and parsing already asked.
    A workflow or confirm step is credited when *old* completed a step with the
    same kind, text, and set of PRs landed before it — so adding, removing, or
    reordering steps keeps credit, while changing a step's text, or landing a
    different set of PRs ahead of it, makes it run again.

    Returns the indices (into ``old.plan``) of completed checkpoints that found
    no match in *plan*, so the caller can show what does not carry over.
    """
    unmatched: dict[_CheckpointKey, list[int]] = {}
    for index, key in enumerate(_checkpoint_keys(old.plan, old.stack)):
        if key is not None and _checkpoint_done(old.plan[index]):
            unmatched.setdefault(key, []).append(index)

    for step, key in zip(plan, _checkpoint_keys(plan, stack)):
        if key is None or not unmatched.get(key):
            continue
        previous = old.plan[unmatched[key].pop(0)]
        # A step parsing already credited (inside the landed prefix) keeps that
        # credit, but still consumes its match so it isn't reported as lost.
        if not _checkpoint_done(step):
            _credit(step, previous)

    return sorted(i for indices in unmatched.values() for i in indices)


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


# The conventional suffix for a landing plan. Plans autoland writes carry it,
# and editors key off it (see editors/vscode) to highlight the plan syntax.
PLAN_SUFFIX = ".autoland-plan"


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
    *,
    initial_text: str | None = None,
) -> list[PlanStep]:
    """Open a plan in $EDITOR and return the parsed result.

    The editor starts from *initial_text* when given (``--replan -i`` passes the
    plan being replaced), and from the default plan otherwise.
    """
    plan_text = initial_text or format_plan_for_editor(
        stack, generate_default_plan(stack, default_workflow, count)
    )
    editor = os.environ.get("EDITOR", "vim")

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=PLAN_SUFFIX, prefix="autoland-plan-", delete=False
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


class _Outcome(Enum):
    """The broad state a step is in, independent of how it is drawn."""

    DONE = "done"
    ACTIVE = "active"
    PENDING = "pending"
    FAILED = "failed"


# Every plan line starts with an icon, so a plan scans as a column of glyphs
# before any of the words are read. Column alignment depends on the widths
# below holding for whichever set is in use, so both sets are fixed-width:
# _ICON_CELLS for the four outcome icons, _POINTER_CELLS for the pointer.
_POINTER_CELLS = 1
_ICON_CELLS = 2


@dataclass(frozen=True)
class _Glyphs:
    done: str
    active: str
    pending: str
    failed: str
    pointer: str  # marks the next step with work left to do
    detail: str  # bullet for a step's second line
    sep: str  # between the fragments of a headline or detail line


# The emoji are all East Asian Wide, so they occupy _ICON_CELLS on their own.
_UNICODE_GLYPHS = _Glyphs(
    done="✅",
    active="⏳",
    pending="⬜",
    failed="❌",
    pointer="→",
    detail="↳",
    sep=" · ",
)

# Used when stdout cannot encode the emoji. The status column names every state
# in words anyway, so the substitutes only have to be distinguishable.
_ASCII_GLYPHS = _Glyphs(
    done="OK",
    active="..",
    pending="  ",
    failed="!!",
    pointer=">",
    detail="*",
    sep=" - ",
)


def _pick_glyphs() -> _Glyphs:
    """The richest glyph set stdout can actually encode.

    Writing an un-encodable character raises UnicodeEncodeError, which would
    take down the whole landing run — print_status is called from the SIGINT
    handler and from _finish, so even a successful autoland would die on the
    way out, leaving its checkpoint and worktree behind.
    """
    encoding = getattr(sys.stdout, "encoding", None) or "ascii"
    try:
        "".join(astuple(_UNICODE_GLYPHS)).encode(encoding)
    except (UnicodeEncodeError, LookupError):
        return _ASCII_GLYPHS
    return _UNICODE_GLYPHS


GLYPHS = _pick_glyphs()


def _icon(outcome: _Outcome) -> str:
    """The current glyph set's icon for *outcome*.

    Looked up per render rather than baked into the tables below, so a test can
    swap GLYPHS.
    """
    return str(getattr(GLYPHS, outcome.value))


_PR_STATE_OUTCOMES = {
    PRState.PENDING: _Outcome.PENDING,
    PRState.WAITING_FOR_APPROVAL: _Outcome.ACTIVE,
    PRState.WAITING_FOR_CHECKS: _Outcome.ACTIVE,
    PRState.IN_MERGE_QUEUE: _Outcome.ACTIVE,
    PRState.WAITING_FOR_WORKFLOW: _Outcome.ACTIVE,
    PRState.MERGED: _Outcome.DONE,
    PRState.FAILED: _Outcome.FAILED,
}

# "skipped" is set only by _mark_skipped, i.e. for a workflow that sits inside a
# plan's already-landed prefix — it must have run before those PRs could merge,
# so the plan assumes it did rather than claiming it was deliberately skipped.
_WORKFLOW_STEP_STATUS = {
    "pending": (_Outcome.PENDING, "Pending", "dim"),
    "waiting": (_Outcome.ACTIVE, "Waiting for workflow", "blue"),
    "succeeded": (_Outcome.DONE, "Workflow complete", "green"),
    "failed": (_Outcome.FAILED, "Failed", "red bold"),
    "skipped": (_Outcome.DONE, "Assumed completed", "green"),
}

_STEP_KINDS = {LandStep: "Land", WorkflowStep: "Workflow", ConfirmStep: "Confirm"}


def _land_entry(ctx: LandingContext, step: LandStep) -> StackEntry | None:
    """The stack entry a land step targets, or None if its PR already landed."""
    if step.already_landed:
        return None
    return ctx.stack[step.entry_index]


@dataclass(frozen=True)
class _StepRow:
    """One plan step, flattened into the fields the display lays out."""

    is_next: bool  # the first step with work left to do
    outcome: _Outcome
    number: str  # "1.", "2." ...
    status: str
    status_style: str
    kind: str  # Land / Workflow / Confirm
    key: str  # "#103", "deploy.yaml", the confirm condition
    note: str  # PR title, or "" when there is nothing to add
    detail: str  # second line: error message, retry counts, ...

    @property
    def done(self) -> bool:
        return self.outcome is _Outcome.DONE


def _pointer_index(ctx: LandingContext, plan: list[PlanStep]) -> int:
    """Index of the step the pointer marks: the first with work left to do.

    A plan replayed against a partially-landed stack opens with a prefix of
    steps that are already satisfied (see ``landed_prefix_end``). Pointing at
    step 1 there reads as "about to redo work that shipped last week", so the
    pointer starts past the prefix. Once a run is under way ``current_step`` has
    moved further along and wins.
    """
    return max(ctx.current_step, landed_prefix_end(plan) + 1)


def _step_status(
    step: PlanStep,
    ctx: LandingContext,
    *,
    index: int,
    plan: list[PlanStep],
    pointer: int,
) -> tuple[_Outcome, str, str]:
    """The (outcome, label, style) a step shows in the status column."""
    if isinstance(step, LandStep):
        if step.already_landed:
            return _Outcome.DONE, "Landed", "green"
        entry = ctx.stack[step.entry_index]
        return (
            _PR_STATE_OUTCOMES.get(entry.state, _Outcome.PENDING),
            STATE_LABELS.get(entry.state, str(entry.state)),
            STATE_STYLES.get(entry.state, ""),
        )
    if isinstance(step, WorkflowStep):
        return _WORKFLOW_STEP_STATUS.get(
            step.state, (_Outcome.PENDING, "Pending", "dim")
        )
    # ConfirmStep. A confirmation inside the landed prefix was never actually
    # given in this run — it is inferred from the fact that later PRs merged —
    # so say so rather than claiming the user confirmed it.
    if step.confirmed:
        if is_assumed_completed(plan, index):
            return _Outcome.DONE, "Assumed completed", "green"
        return _Outcome.DONE, "Confirmed", "green"
    if index == pointer:
        return _Outcome.ACTIVE, "Awaiting confirmation", "yellow"
    return _Outcome.PENDING, "Pending", "dim"


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
    return GLYPHS.sep.join(parts)


def _plan_rows(ctx: LandingContext) -> list[_StepRow]:
    plan = ctx.plan or generate_default_plan(ctx.stack)
    pointer = _pointer_index(ctx, plan)
    rows = []
    for index, step in enumerate(plan):
        outcome, status, style = _step_status(
            step, ctx, index=index, plan=plan, pointer=pointer
        )
        key, note = _step_target(step, ctx)
        rows.append(
            _StepRow(
                is_next=index == pointer,
                outcome=outcome,
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
    return GLYPHS.sep.join(
        [
            "Autoland plan",
            f"{len(rows)} {noun}",
            f"{done} done",
            f"{len(rows) - done} remaining",
        ]
    )


def _column_widths(rows: list[_StepRow]) -> tuple[int, int, int]:
    """Widths of the number, status, and kind columns."""
    return (
        max((len(r.number) for r in rows), default=0),
        max((len(r.status) for r in rows), default=0),
        max((len(r.kind) for r in rows), default=0),
    )


def _detail_indent(num_width: int) -> int:
    """Cell the detail line's bullet starts at: under the status column.

    Counted in terminal cells, not characters — the outcome icons are wide
    glyphs, so len() would undercount them and shift the line left.
    """
    return _POINTER_CELLS + 1 + _ICON_CELLS + 1 + num_width + 1


def _header_segments(
    row: _StepRow, widths: tuple[int, int, int]
) -> list[tuple[str, str]]:
    """A step's header line as (text, style) pairs.

    The one place the layout is defined; both renderers consume it, so the
    plain fallback cannot drift away from the rich output.
    """
    num_w, status_w, kind_w = widths
    dim = "dim " if row.done else ""
    segments = [
        (f"{GLYPHS.pointer if row.is_next else ' ':<{_POINTER_CELLS}} ", "bold cyan"),
        (f"{_icon(row.outcome)} ", ""),
        (f"{row.number:<{num_w}} ", f"{dim}bold"),
        (f"{row.status:<{status_w}}", row.status_style),
        (" | ", "dim"),
        (f"{row.kind:<{kind_w}}", f"{dim}bold"),
        (" | ", "dim"),
        (row.key, f"{dim}cyan"),
    ]
    if row.note:
        segments.append((f" - {row.note}", dim.strip()))
    return segments


def render_status_rich(ctx: LandingContext) -> Group:  # pragma: no cover
    """Render plan progress as compact, styled lines (rich-only path).

    Text is appended rather than parsed as markup, so a PR title containing
    something like "[1,25]" renders literally instead of being read as a style.
    """
    rows = _plan_rows(ctx)
    widths = _column_widths(rows)
    indent = _detail_indent(widths[0])

    # list[Any]: the rich renderable protocol isn't importable on the no-rich
    # path, and this function only runs when rich is present.
    parts: list[Any] = [Text(_plan_headline(rows), style="bold"), Text()]
    for row in rows:
        # Only the header line refuses to wrap: a long PR title would otherwise
        # fold back to column 0 and destroy the alignment the format is built
        # on, and the plan is a scannable overview — the full title is on the
        # PR. Detail and abort lines wrap normally, because a truncated error
        # message is the one thing a failed run cannot afford to lose.
        line = Text(no_wrap=True, overflow="ellipsis")
        for text, style in _header_segments(row, widths):
            line.append(text, style=style)
        parts.append(line)
        if row.detail:
            detail = Text(f"{GLYPHS.detail} {row.detail}", style="dim")
            # expand=False so the pad doesn't fill the rest of the line with
            # blanks; a wrapped detail still stays under the indent.
            parts.append(Padding(detail, (0, 0, 0, indent), expand=False))
    if ctx.aborted:
        parts += [Text(), Text(f"ABORTED: {ctx.abort_reason}", style="red bold")]
    return Group(*parts)


def render_status_plain(ctx: LandingContext) -> str:
    """Render plan progress as plain text (the no-rich fallback)."""
    rows = _plan_rows(ctx)
    widths = _column_widths(rows)
    indent = " " * _detail_indent(widths[0])

    lines = ["", _plan_headline(rows), ""]
    for row in rows:
        lines.append("".join(text for text, _ in _header_segments(row, widths)))
        if row.detail:
            lines.append(f"{indent}{GLYPHS.detail} {row.detail}")
    if ctx.aborted:
        lines += ["", f"ABORTED: {ctx.abort_reason}"]
    return "\n".join(lines)


def print_status(ctx: LandingContext) -> None:
    if HAVE_RICH:
        console.print(render_status_rich(ctx))
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
    """A one-line, human-readable description of a plan step.

    Prose for the confirm prompt's "Next steps" list, built from the same
    (key, note) the status panel shows, so the two cannot name a step
    differently.
    """
    key, note = _step_target(step, ctx)
    if isinstance(step, LandStep):
        if step.already_landed:
            return f"Land PR {key} (already landed)"
        return f"Land PR {key}: {note}"
    if isinstance(step, WorkflowStep):
        return f"Wait for workflow {key}"
    # ConfirmStep
    if step.condition:
        return f"Manual confirmation: {key}"
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


# ---------------------------------------------------------------------------
# Merging a run of land steps as one GitHub stack
# ---------------------------------------------------------------------------


def native_stack_run(ctx: LandingContext, start: int) -> list[StackEntry]:
    """The PRs a stack merge starting at plan step *start* would land.

    That is the run of consecutive ``l`` steps from *start* whose PRs are still
    open. A ``w`` or ``c`` step ends the run: it has to see the PRs above it
    land separately from the ones below it. A run of fewer than two PRs gains
    nothing from a stack merge, so it comes back empty.
    """
    entries: list[StackEntry] = []
    for step in ctx.plan[start:]:
        if not isinstance(step, LandStep) or step.already_landed:
            break
        entry = ctx.stack[step.entry_index]
        if entry.state == PRState.MERGED:
            break
        entries.append(entry)
    return entries if len(entries) > 1 else []


def native_stack_runs(ctx: LandingContext) -> list[tuple[int, list[StackEntry]]]:
    """Every ``(first step, PRs)`` run the rest of the plan would stack-merge."""
    runs = []
    index = ctx.current_step
    while index < len(ctx.plan):
        entries = native_stack_run(ctx, index)
        if entries:
            runs.append((index, entries))
        index += max(len(entries), 1)
    return runs


def print_native_stack_runs(ctx: LandingContext, opts: AutolandOptions) -> None:
    if not opts.merge_as_stack:
        return
    for first, entries in native_stack_runs(ctx):
        prs = ", ".join(f"#{e.pr_number}" for e in entries)
        console.print(
            f"[dim]Steps {first + 1}-{first + len(entries)} ({prs}) will merge "
            "together as a GitHub stack.[/dim]"
        )


def _open_native_stack_prs(native_stack: dict) -> list[int]:
    """The numbers of a GitHub stack's unmerged PRs, bottom first."""
    return [
        pr["number"]
        for pr in native_stack.get("pull_requests", [])
        if not pr.get("merged_at") and pr.get("state", "open") == "open"
    ]


def _native_stack_for(prs: list[int]) -> tuple[int | None, bool]:
    """Find or create a GitHub stack whose bottom open PRs are exactly *prs*.

    Returns ``(stack number, whether it holds just the run)``, with a ``None``
    number when the run can't be merged as a stack. A stack holding just the
    run is ours to dissolve if the merge falls through, even when it predates
    this call: it is most likely one an interrupted run left behind.
    """
    try:
        existing = github.find_native_stack(prs[0])
    except (RuntimeError, json.JSONDecodeError) as e:
        console.print(f"[yellow]Could not look up GitHub stacks: {e}[/yellow]")
        return None, False

    if existing is None:
        try:
            return github.create_native_stack(prs)["number"], True
        except (RuntimeError, json.JSONDecodeError, KeyError, TypeError) as e:
            # A retried POST may have created the stack on its first attempt.
            with contextlib.suppress(RuntimeError, json.JSONDecodeError):
                existing = github.find_native_stack(prs[0])
            if existing is not None and _open_native_stack_prs(existing) == prs:
                return existing["number"], True
            console.print(f"[yellow]Could not create a GitHub stack: {e}[/yellow]")
            return None, False

    # A stack made earlier — by an interrupted run, or by hand with gh stack —
    # is reusable if the run sits at its bottom: merging the run's top PR then
    # merges exactly the run. Anything else would merge PRs the plan doesn't.
    open_prs = _open_native_stack_prs(existing)
    if open_prs[: len(prs)] == prs:
        return existing["number"], open_prs == prs
    console.print(
        f"[yellow]PR #{prs[0]} is already in GitHub stack #{existing['number']}, "
        "which does not match the plan.[/yellow]"
    )
    return None, False


@dataclass
class NativeStackMergeResult:
    landed: bool = False  # every PR in the run merged
    # Stop the plan. Otherwise, the PRs of the run that are still open should be
    # landed one at a time instead.
    abort_reason: str = ""


def _await_native_stack_merge(
    entries: list[StackEntry],
    uuid: str | None,
    *,
    opts: AutolandOptions,
    ctx: LandingContext,
) -> str:
    """Wait for a requested stack merge to finish.

    Returns "" once every PR merged, else why it didn't. A failure may still
    have merged some of the PRs: GitHub stops a stack merge at the PR that
    failed, and keeps the ones below it.
    """
    top = entries[-1]
    awake_elapsed = 0.0
    while True:
        if ctx.aborted:
            return "aborted"
        if awake_elapsed > opts.merge_timeout:
            return "timed out waiting for the stack to merge"

        still_open = []
        for entry in entries:
            if entry.state == PRState.MERGED:
                continue
            state = github.pr_state(entry.pr_number)
            if state == "MERGED":
                entry.state = PRState.MERGED
                entry.error_message = ""
            elif state == "CLOSED":
                return f"PR #{entry.pr_number} was closed"
            else:
                still_open.append(entry)
        if not still_open:
            return ""

        status, message = "", ""
        if uuid:
            with contextlib.suppress(RuntimeError, json.JSONDecodeError):
                status, message = github.merge_async_status(top.pr_number, uuid)
        if status == "failed":
            return message or "GitHub reported the stack merge as failed"
        # Every PR enters the queue together, and GitHub drops the PRs above any
        # PR that leaves it, so the lowest open PR leaving means the merge is
        # off. A request still "pending" hasn't reached the queue yet; without
        # a request id to ask, give it one interval to get there.
        settled = status == "enqueued" or (not uuid and awake_elapsed > 0)
        if settled and not github.in_merge_queue(still_open[0].pr_number):
            return f"PR #{still_open[0].pr_number} was booted from the merge queue"

        mins = int(awake_elapsed) // 60
        for entry in still_open:
            entry.error_message = f"Merging as a stack ({mins}m elapsed)..."
        console.print(
            f"[dim]Stack of {len(entries)} PRs up to #{top.pr_number}: "
            f"{status or 'merging'} ({mins}m) — polling in {opts.poll_interval}s"
            "[/dim]"
        )
        resilient_sleep(opts.poll_interval)
        awake_elapsed += opts.poll_interval


def land_as_native_stack(
    entries: list[StackEntry],
    *,
    ctx: LandingContext,
    common: cli.CommonArgs,
    opts: AutolandOptions,
) -> NativeStackMergeResult:
    """Land a run of consecutive PRs (bottom first) with one stack merge.

    Every PR still needs its own approval and passing checks, so those are
    waited for first, exactly as when landing one at a time. Then a single
    merge request on the top PR merges the whole run — through the merge queue
    as one group, where the repo has one — and the stack above is rebased and
    re-submitted once, not once per PR.

    Anything that keeps the run from merging as a stack is not fatal: the
    result asks for the rest to be landed one at a time. A GitHub stack holding
    just the run is dissolved first, since GitHub only lets stacked PRs merge
    through the stack merge API.
    """
    prs = [e.pr_number for e in entries]
    top = entries[-1]
    console.print(
        f"\n{'=' * 60}\n[bold]Landing {len(entries)} PRs as a GitHub stack: "
        f"{', '.join(f'#{n}' for n in prs)}[/bold]\n{'=' * 60}"
    )

    native_stack_number, dissolvable = _native_stack_for(prs)
    if native_stack_number is None:
        console.print("[yellow]Landing these PRs one at a time instead.[/yellow]")
        return NativeStackMergeResult()

    failure = ""
    abort_reason = ""
    for entry in entries:
        if not wait_for_approval(entry, opts=opts, ctx=ctx):
            abort_reason = f"PR #{entry.pr_number} approval wait was aborted"
            break
    else:
        for entry in entries:
            if entry.state == PRState.MERGED:
                continue
            if not wait_for_checks(entry, opts=opts, ctx=ctx):
                abort_reason = f"PR #{entry.pr_number} checks failed after retries"
                break

    bottom = next((e for e in entries if e.state != PRState.MERGED), None)
    # Only the lowest open PR is checked here: GitHub reports the ones above it
    # as blocked until it merges. The merge request itself enforces every PR's
    # requirements.
    if (
        not abort_reason
        and bottom is not None
        and not wait_for_mergeable(bottom, opts=opts, ctx=ctx).ready
    ):
        abort_reason = f"PR #{bottom.pr_number} failed to merge"

    if not abort_reason and bottom is not None:
        for entry in entries:
            if entry.state != PRState.MERGED:
                entry.state = PRState.IN_MERGE_QUEUE
                entry.error_message = "Merging as a stack..."
        console.print(
            f"\n[bold cyan]Merging PRs #{bottom.pr_number}-#{top.pr_number} "
            f"as a stack[/bold cyan]"
        )
        uuid: str | None = None
        # The PR above the run is based on the run's top branch. When that
        # branch is deleted after the merge, GitHub retargets the PR to the top
        # PR's base, which is another of the run's branches, deleted along with
        # it, so GitHub closes the PR. Basing it on the target up front keeps it
        # open; the resubmit after the merge restores its real base. A PR above
        # that is in the GitHub stack itself is GitHub's to retarget.
        above = ctx.stack.index(top) + 1
        if dissolvable and above < len(ctx.stack):
            try:
                github.set_base(ctx.stack[above].pr_number, common.target)
            except RuntimeError as e:
                failure = (
                    f"could not base PR #{ctx.stack[above].pr_number} on "
                    f"{common.target}: {e}"
                )
        if not failure:
            try:
                # Ask GitHub rather than trust the config: the request body must
                # match whether the branch really has a queue (see merge_async).
                has_queue = github.has_merge_queue(common.target)
                uuid = github.merge_async(
                    top.pr_number,
                    merge_queue=opts.merge_queue if has_queue is None else has_queue,
                )
            except (RuntimeError, json.JSONDecodeError) as e:
                # 409: a merge request for this stack is already in flight (e.g.
                # from a run that was interrupted) — wait for it like our own.
                if "HTTP 409" not in str(e):
                    failure = f"stack merge request failed: {e}"
        if not failure:
            failure = _await_native_stack_merge(entries, uuid, opts=opts, ctx=ctx)
            if failure == "aborted":
                abort_reason = "Stack merge was aborted"

    landed = all(e.state == PRState.MERGED for e in entries)
    if not landed and dissolvable:
        # Leave no stack behind: landing one PR at a time only works on PRs
        # that aren't in one. Queued PRs stay queued.
        try:
            github.unstack_native_stack(native_stack_number)
        except (RuntimeError, json.JSONDecodeError) as e:
            console.print(
                f"[yellow]Could not dissolve GitHub stack #{native_stack_number}: {e}[/yellow]"
            )

    merged = [e for e in entries if e.state == PRState.MERGED]
    if failure and not abort_reason:
        console.print(
            f"[yellow]Stack merge did not complete ({failure}). Landing the "
            "remaining PRs one at a time.[/yellow]"
        )
        for entry in entries:
            if entry.state != PRState.MERGED:
                entry.state = PRState.PENDING
                entry.error_message = f"Stack merge: {failure}"
    elif landed:
        console.print(
            f"\n[bold green]PRs {', '.join(f'#{n}' for n in prs)} merged![/bold green]"
        )

    if merged:
        _refresh_last_landed_sha(ctx, common, merged[-1].pr_number)
        if ctx.stack.index(merged[-1]) < len(ctx.stack) - 1:
            try:
                rebase_and_resubmit(common)
            except Exception as e:  # noqa: BLE001 - report any resubmit failure
                return NativeStackMergeResult(
                    landed=landed,
                    abort_reason=(
                        f"Rebase failed after merging #{merged[-1].pr_number}: {e}"
                    ),
                )
    return NativeStackMergeResult(landed=landed, abort_reason=abort_reason)


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
    # The last step of a run whose stack merge fell through; the rest of that
    # run lands one PR at a time rather than retrying the stack merge.
    unstacked_through = -1

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

            run_entries = (
                native_stack_run(ctx, step_idx)
                if opts.merge_as_stack and step_idx > unstacked_through
                else []
            )
            if run_entries:
                result = land_as_native_stack(
                    run_entries, ctx=ctx, common=common, opts=opts
                )
                if result.abort_reason:
                    return _abort(ctx, checkpointer, result.abort_reason)
                if not result.landed:
                    unstacked_through = step_idx + len(run_entries) - 1
                checkpointer.save(ctx)

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
            # "succeeded" before the loop reaches it means --replan carried the
            # result over from the run this one replaced.
            if step.state in ("skipped", "succeeded"):
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
        "--merge-as-stack",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=(
            "Merge each run of consecutive 'l' steps in one go, as a GitHub "
            "stack, instead of one PR at a time (config: autoland.merge_as_stack; "
            "default: on)."
        ),
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
            "'l' / 'w <workflow>' / 'c [condition]' lines). Plans conventionally "
            f"use the {PLAN_SUFFIX} suffix."
        ),
    )
    run_mode = p.add_mutually_exclusive_group()
    run_mode.add_argument(
        "--resume",
        action="store_true",
        help="Resume a previously interrupted run from its checkpoint.",
    )
    run_mode.add_argument(
        "--replan",
        action="store_true",
        help=(
            "Replace the plan of an interrupted or running autoland and continue, "
            "keeping its progress: landed PRs, finished workflows, and given "
            "confirmations carry over. Re-reads the run's plan file, or the one "
            "given with --plan-file, or opens the current plan with -i. Stops a "
            "running autoland first (after asking)."
        ),
    )
    run_mode.add_argument(
        "--status",
        action="store_true",
        help=(
            "Show whether an autoland is in progress for the branch, where its "
            "state file is, and its progress as of the last checkpoint. Changes "
            "nothing."
        ),
    )
    p.add_argument(
        "-o",
        "--output",
        choices=["text", "json"],
        default="text",
        help="Output format for --status (default: text).",
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

    # Purely informational, so it works whatever the repo's landing strategy.
    output = getattr(args, "output", "text")
    if getattr(args, "status", False):
        show_status(opts, output=output)
        return
    if output != "text":
        console.print("[red]-o/--output only applies to --status.[/red]")
        sys.exit(1)

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
            "restores its plan from the checkpoint. To continue with a new plan, "
            "use --replan --plan-file.[/red]"
        )
        sys.exit(1)
    if opts.replan and opts.count is not None:
        console.print(
            "[red]--replan and --count can't be combined: the plan being "
            "replaced already specifies which PRs to land.[/red]"
        )
        sys.exit(1)

    if opts.replan:
        _run_replan(common, opts)
        return
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
    # Installed as landing starts: from here on, the handler and _finish
    # decide whether the worktree is kept.
    if worktree is not None:
        worktree.landing = True

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


def _ask_replan_or_overwrite(state_path: Path) -> str | None:
    """Ask what to do about an existing checkpoint when starting a new run.

    Returns "replan", "overwrite", or None to abort. Replanning is the default:
    it is the safe choice, and it previews the result before running anything.
    """
    console.print(
        "\n[bold yellow]An autoland is already in progress for this "
        "branch.[/bold yellow]\n"
        f"[yellow]A checkpoint from that run exists at {state_path}.[/yellow]\n\n"
        "  [bold]r[/bold]  replan: keep its progress (landed PRs, finished "
        "workflows, given confirmations) and continue with this plan\n"
        "  [bold]o[/bold]  overwrite: discard that progress and start over\n"
    )
    try:
        answer = (
            console.input(
                "[yellow]Choose r or o (Enter = r; anything else aborts): [/yellow]"
            )
            .strip()
            .lower()
        )
    except EOFError:
        return None
    if answer in ("", "r"):
        return "replan"
    if answer == "o":
        return "overwrite"
    return None


def _run_fresh(common: cli.CommonArgs, opts: AutolandOptions) -> None:
    branch = opts.branch or _current_branch()
    state_path = opts.state_file or AutolandCheckpointer.default_path(branch)

    # A dry run only previews the plan; it neither writes state nor competes for
    # the lock, so let it run freely alongside a real autoland.
    lock: AutolandLock | None = None
    replan = False
    if not opts.dry_run:
        lock = AutolandLock.for_state(state_path)
        if not lock.acquire():
            # A run that has not checkpointed yet has no progress to replan
            # from, so there is nothing to offer but waiting.
            if not state_path.exists():
                console.print(
                    f"[red]An autoland is already running for branch "
                    f"[bold]{branch}[/bold]. Wait for it to finish before "
                    "starting another.[/red]"
                )
                sys.exit(1)
            if not _stop_running_autoland(lock):
                sys.exit(1)
            replan = True

    worktree: Worktree | None = None
    try:
        # An existing state file means a previous run was interrupted and can be
        # resumed; starting fresh would clobber it, so ask first.
        if lock is not None and state_path.exists() and not replan:
            choice = _ask_replan_or_overwrite(state_path)
            if choice is None:
                console.print(
                    "[red]Aborted — the previous autoland is untouched.[/red]"
                )
                return
            replan = choice == "replan"
        if replan:
            _replan(common, opts, state_path)
            return

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
            path=state_path,
            branch=branch,
            base=common.target,
            plan_file=opts.plan_file,
        )

        print_status(ctx)
        print_native_stack_runs(ctx, opts)
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
        if worktree is not None:
            worktree.remove_unless_landing()
        if lock is not None:
            lock.release()


def _state_path(opts: AutolandOptions) -> Path:
    """The checkpoint an existing run for *opts* would have written."""
    if opts.state_file:
        return opts.state_file
    return AutolandCheckpointer.default_path(opts.branch or _current_branch())


def _run_resume(common: cli.CommonArgs, opts: AutolandOptions) -> None:
    sf_path = _state_path(opts)

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

    worktree: Worktree | None = None
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
        print_native_stack_runs(ctx, opts)
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
        if worktree is not None:
            worktree.remove_unless_landing()
        lock.release()


# ---------------------------------------------------------------------------
# Replanning (`autoland --replan`)
# ---------------------------------------------------------------------------

# How long to wait for a stopped autoland to save its checkpoint and exit.
_TAKEOVER_TIMEOUT = 60


def _stop_running_autoland(lock: AutolandLock) -> bool:
    """Offer to stop the autoland holding *lock*, then take the lock over.

    The other run is sent SIGINT, the same as Ctrl+C in its terminal: it saves
    its checkpoint and exits, releasing the lock. Returns True once this process
    holds *lock*, or False if the user declined or the run could not be stopped.
    """
    pid = lock.holder_pid()
    if pid is None:
        console.print(
            "[red]An autoland is running for this branch, but its PID is unknown "
            "(it was started by an older stack-pr). Stop it with Ctrl+C in its "
            "terminal, then re-run this command.[/red]"
        )
        return False
    console.print(
        f"\n[bold yellow]An autoland is running for this branch "
        f"(pid {pid}).[/bold yellow]\n"
        "[yellow]Replanning stops it the way Ctrl+C in its terminal would — it "
        "saves its checkpoint and exits — and continues from there in this "
        "terminal.[/yellow]\n"
    )
    try:
        answer = console.input(
            "[yellow]Stop it and replan? Type y/Y to confirm (anything else "
            "aborts): [/yellow]"
        ).strip()
    except EOFError:
        answer = ""
    if answer not in ("y", "Y"):
        console.print("[red]Aborted — the running autoland is untouched.[/red]")
        return False

    try:
        os.kill(pid, signal.SIGINT)
    except ProcessLookupError:
        pass  # it exited on its own meanwhile; the lock is (about to be) free
    except PermissionError as e:
        console.print(f"[red]Could not stop pid {pid}: {e}[/red]")
        return False

    console.print(f"[dim]Waiting for pid {pid} to save its checkpoint...[/dim]")
    deadline = time.monotonic() + _TAKEOVER_TIMEOUT
    while time.monotonic() < deadline:
        if lock.acquire():
            return True
        time.sleep(0.5)
    console.print(
        f"[red]pid {pid} still holds the lock after {_TAKEOVER_TIMEOUT}s. Stop it "
        "in its terminal, then re-run this command.[/red]"
    )
    return False


def _unpushed_changes(raw: list[cli.StackEntry], common: cli.CommonArgs) -> list[str]:
    """Commits in the stack whose code GitHub doesn't have, for a warning.

    Replanning usually follows a code change, and landing a PR whose branch
    predates that change would ship the old code.
    """
    problems = []
    for e in raw:
        name = f"{e.commit.commit_id()[:8]} {e.commit.title()}"
        if not e.has_pr():
            problems.append(f"{name}: no PR yet")
            continue
        pushed = run(
            ["git", "rev-parse", "--verify", "--quiet", f"{common.remote}/{e.head}"],
            check=False,
            quiet=True,
            retries=0,
        ).stdout.strip()
        if pushed and pushed != e.commit.commit_id():
            problems.append(f"{name}: local commit differs from #{cli.last(e.pr)}")
    return problems


def _describe_lost(old: LandingContext, index: int) -> str:
    """One not-carried-over checkpoint, with the PRs it was recorded after."""
    key = _checkpoint_keys(old.plan, old.stack)[index]
    landed = sorted(key[2]) if key else []
    after = (
        "after " + ", ".join(f"#{pr}" for pr in landed)
        if landed
        else "before any PR landed"
    )
    return f"{_describe_step(old.plan[index], old)} ({after})"


def _replacement_plan(
    opts: AutolandOptions,
    old_checkpointer: AutolandCheckpointer,
    old: LandingContext,
    stack: list[StackEntry],
) -> tuple[list[PlanStep], Path | None]:
    """The new plan for a replan, and the plan file it came from (if any)."""
    if opts.interactive:
        # Start the editor from the plan being replaced, not the default one.
        initial = format_plan_for_editor(old.stack, old.plan)
        return edit_plan_interactive(stack, initial_text=initial), None
    plan_file = opts.plan_file or old_checkpointer.plan_file
    if plan_file is not None:
        console.print(f"[dim]Plan: {plan_file}[/dim]")
        return plan_from_file(plan_file, stack), plan_file
    # The run had no plan file (a default or -i plan): replay the saved plan.
    try:
        return parse_plan(format_plan_for_editor(old.stack, old.plan), stack), None
    except ValueError as e:
        console.print(
            f"[red]The saved plan no longer fits the stack: {e}\n"
            "Give a new plan with --plan-file or -i.[/red]"
        )
        sys.exit(1)


def _replan(common: cli.CommonArgs, opts: AutolandOptions, state_path: Path) -> None:
    """Replace the plan of the run checkpointed at *state_path*, keep its
    progress, and continue.

    The caller holds the lock, except on a dry run, which only previews.
    """
    try:
        old_checkpointer, old = AutolandCheckpointer.load(state_path)
    except (OSError, ValueError, KeyError) as e:
        console.print(f"[red]Failed to load state file {state_path}: {e}[/red]")
        sys.exit(1)
    branch = old_checkpointer.branch
    if opts.branch and opts.branch != branch:
        console.print(
            f"[red]--branch {opts.branch} does not match saved branch {branch}[/red]"
        )
        sys.exit(1)

    console.print(f"[bold]Replanning from checkpoint: [cyan]{state_path}[/cyan][/bold]")
    worktree: Worktree | None = None
    try:
        if opts.branch or branch != _current_branch():
            worktree = Worktree(branch)
            worktree.create()
        # As in _run_fresh: deduce against the (possibly worktree) HEAD.
        common = cli.deduce_base(common)

        # The code may have changed since the checkpoint, so the stack is
        # rediscovered rather than restored.
        console.print("\n[bold]Rediscovering stack...[/bold]\n")
        raw = cli.get_stack(base=common.base, head=common.head, verbose=common.verbose)
        stack = _stack_entries(raw)
        if not stack:
            console.print("[red]No stack found on the current branch.[/red]")
            sys.exit(1)
        enrich_stack(stack)

        plan, plan_file = _replacement_plan(opts, old_checkpointer, old, stack)
        lost = carry_over_progress(old, plan, stack)
        ctx = LandingContext(stack=stack, plan=plan)

        print_status(ctx)
        print_native_stack_runs(ctx, opts)
        if lost:
            console.print(
                "\n[yellow]Done in the previous run, but not carried over (changed, "
                "removed, or now after different PRs):[/yellow]"
            )
            for index in lost:
                console.print(f"  - {_escape_markup(_describe_lost(old, index))}")
        unpushed = _unpushed_changes(raw, common)
        if unpushed:
            console.print(
                "\n[bold yellow]Warning: GitHub doesn't have all of this stack's code. "
                "Run `stack-pr submit` first if you changed it:[/bold yellow]"
            )
            for problem in unpushed:
                console.print(f"  - {_escape_markup(problem)}")

        if opts.dry_run:
            console.print("\n[yellow]Dry run — exiting.[/yellow]")
            _dispose_worktree(worktree, opts, succeeded=True)
            return
        try:
            answer = console.input(
                "\n[yellow]Continue with this plan? Type y/Y to confirm (anything "
                "else aborts): [/yellow]"
            ).strip()
        except EOFError:
            answer = ""
        if answer not in ("y", "Y"):
            console.print("[red]Aborted — the previous checkpoint is untouched.[/red]")
            _dispose_worktree(worktree, opts, succeeded=True)
            return

        checkpointer = AutolandCheckpointer(
            path=state_path, branch=branch, base=common.target, plan_file=plan_file
        )
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
        if worktree is not None:
            worktree.remove_unless_landing()


def _run_replan(common: cli.CommonArgs, opts: AutolandOptions) -> None:
    state_path = _state_path(opts)
    if not state_path.exists():
        console.print(
            f"[red]No autoland to replan: no state file at {state_path}. Start "
            "one with --plan-file or -i.[/red]"
        )
        sys.exit(1)
    # A dry run only previews, so it neither competes for the lock nor stops a
    # running autoland: it is how to check a replan before committing to it.
    if opts.dry_run:
        _replan(common, opts, state_path)
        return
    lock = AutolandLock.for_state(state_path)
    if not lock.acquire() and not _stop_running_autoland(lock):
        sys.exit(1)
    try:
        _replan(common, opts, state_path)
    finally:
        lock.release()


# ---------------------------------------------------------------------------
# Status report (`autoland --status`)
# ---------------------------------------------------------------------------


def _format_age(seconds: float) -> str:
    """A coarse "how long ago" for a checkpoint's timestamp."""
    minutes = int(seconds) // 60
    if minutes < 1:
        return "just now"
    if minutes < 60:  # noqa: PLR2004
        return f"{minutes}m ago"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:  # noqa: PLR2004
        return f"{hours}h {minutes}m ago"
    return f"{hours // 24}d ago"


@dataclass(frozen=True)
class _OtherRun:
    branch: str | None  # None when the state file can't be read
    running: bool
    state_path: Path


@dataclass(frozen=True)
class _StatusReport:
    """Everything `--status` knows about one run, gathered in a single pass.

    Both output formats render from this, so they cannot disagree.
    """

    state_path: Path
    lock_path: Path
    running: bool
    pid: int | None
    branch: str
    base: str
    ctx: LandingContext | None  # None when there is no checkpoint
    saved_at: float | None  # the checkpoint's mtime
    resume_command: str | None  # None unless the run is stopped and resumable
    others: list[_OtherRun]

    @property
    def status(self) -> str:
        if self.running:
            return "in_progress"
        return "stopped" if self.ctx is not None else "none"


def _resume_command(opts: AutolandOptions) -> str:
    cmd = "stack-pr autoland --resume"
    if opts.state_file:
        return f"{cmd} --state-file {opts.state_file}"
    if opts.branch:
        return f"{cmd} --branch {opts.branch}"
    return cmd


def _other_runs(exclude: Path) -> list[_OtherRun]:
    """Checkpoints for other branches, so a forgotten run is findable."""
    state_dir = AutolandCheckpointer.default_path("_").parent
    runs = []
    for path in sorted(p for p in state_dir.glob("*.json") if p != exclude):
        branch: str | None
        try:
            branch = str(json.loads(path.read_text())["branch"])
        except (OSError, ValueError, KeyError, TypeError):
            branch = None
        runs.append(_OtherRun(branch, AutolandLock.for_state(path).is_held(), path))
    return runs


def _gather_status(opts: AutolandOptions) -> _StatusReport:
    state_path = _state_path(opts)
    lock = AutolandLock.for_state(state_path)
    # Probe once: the run may start or stop while the report is being printed,
    # and the report should describe a single moment.
    running = lock.is_held()
    branch = opts.branch or ("" if opts.state_file else _current_branch())

    ctx: LandingContext | None = None
    base = ""
    saved_at: float | None = None
    if state_path.exists():
        # The checkpoint is replaced by rename, so its stat and content agree.
        saved_at = state_path.stat().st_mtime
        checkpointer, ctx = AutolandCheckpointer.load(state_path)
        branch, base = checkpointer.branch, checkpointer.base

    return _StatusReport(
        state_path=state_path,
        lock_path=lock.path,
        running=running,
        pid=lock.holder_pid() if running else None,
        branch=branch,
        base=base,
        ctx=ctx,
        saved_at=saved_at,
        resume_command=(
            _resume_command(opts) if ctx is not None and not running else None
        ),
        others=_other_runs(exclude=state_path),
    )


def _status_label(report: _StatusReport) -> str:
    if report.running:
        label = "[cyan bold]In progress[/cyan bold]"
        if report.pid is not None:
            label += f" (pid {report.pid})"
        if report.ctx is None:
            label += " — starting up, no checkpoint written yet"
        return label
    return "[yellow bold]Stopped[/yellow bold] — not running, can be resumed"


def _print_status_text(report: _StatusReport) -> None:
    # soft_wrap throughout: a hard-wrapped path can't be copied into a command.
    if report.status == "none":
        target = (
            f"branch [bold]{_escape_markup(report.branch)}[/bold]"
            if report.branch
            else "this state file"
        )
        console.print(f"No autoland in progress for {target}.")
        console.print(
            f"[dim]State file (not present): {report.state_path}[/dim]",
            soft_wrap=True,
        )
    else:
        fields = [
            ("Status", _status_label(report)),
            ("Branch", _escape_markup(report.branch)),
        ]
        if report.base:
            fields.append(("Base", _escape_markup(report.base)))
        fields.append(("State file", _escape_markup(str(report.state_path))))
        if report.running:
            fields.append(("Lock file", _escape_markup(str(report.lock_path))))
        if report.saved_at is not None:
            saved = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(report.saved_at))
            age = _format_age(time.time() - report.saved_at)
            fields.append(("Last saved", f"{saved} ({age})"))

        console.print("[bold]Autoland status[/bold]\n")
        width = max(len(name) for name, _ in fields)
        for name, value in fields:
            console.print(
                f"  [dim]{name + ':':<{width + 1}}[/dim] {value}", soft_wrap=True
            )
        if report.ctx is not None:
            console.print()
            print_status(report.ctx)
        if report.resume_command:
            console.print(
                f"\n[dim]Resume with: {report.resume_command}[/dim]", soft_wrap=True
            )

    if report.others:
        console.print("\n[bold]Other autolands with saved state:[/bold]")
        for other in report.others:
            branch = other.branch or "(unreadable state file)"
            label = (
                "[cyan]in progress[/cyan]"
                if other.running
                else "[yellow]stopped[/yellow]"
            )
            console.print(
                f"  {_escape_markup(branch)} — {label} — "
                f"[dim]{_escape_markup(str(other.state_path))}[/dim]",
                soft_wrap=True,
            )
        console.print(
            "[dim]Inspect one with: stack-pr autoland --status --branch BRANCH[/dim]"
        )


def _step_json(step: PlanStep, row: _StepRow, ctx: LandingContext) -> dict:
    data: dict[str, Any] = {
        "number": int(row.number.rstrip(".")),
        "type": _STEP_TYPES[type(step)],
        "outcome": row.outcome.value,
        "status": row.status,
        "is_next": row.is_next,
    }
    if isinstance(step, LandStep):
        entry = _land_entry(ctx, step)
        data["pr_number"] = entry.pr_number if entry else step.pr_number
        data["pr_url"] = entry.pr_url if entry else None
        data["title"] = entry.title if entry else None
    elif isinstance(step, WorkflowStep):
        data["workflow"] = step.workflow
    else:
        data["condition"] = step.condition
    data["detail"] = row.detail or None
    return data


def _status_json(report: _StatusReport) -> dict:
    """The report as JSON-ready data. Keys are always present; unknowns are null."""
    ctx = report.ctx
    plan = None
    if ctx is not None:
        rows = _plan_rows(ctx)
        done = sum(1 for r in rows if r.done)
        plan = {
            "total": len(rows),
            "done": done,
            "remaining": len(rows) - done,
            "steps": [_step_json(s, r, ctx) for s, r in zip(ctx.plan, rows)],
        }
    return {
        "status": report.status,
        "branch": report.branch or None,
        "base": report.base or None,
        "state_file": str(report.state_path),
        "state_file_exists": ctx is not None,
        "lock_file": str(report.lock_path),
        "pid": report.pid,
        "last_saved": (
            datetime.fromtimestamp(report.saved_at).astimezone().isoformat()
            if report.saved_at is not None
            else None
        ),
        "abort_reason": (ctx.abort_reason or None) if ctx is not None else None,
        "resume_command": report.resume_command,
        "plan": plan,
        "other_runs": [
            {
                "branch": o.branch,
                "status": "in_progress" if o.running else "stopped",
                "state_file": str(o.state_path),
            }
            for o in report.others
        ],
    }


def show_status(opts: AutolandOptions, *, output: str = "text") -> None:
    """Report the autoland state for a branch: whether a run is in progress,
    where its files are, and how far it got.

    Read-only and offline — it never takes the lock, writes the checkpoint, or
    asks GitHub — so it is safe to run alongside a live autoland. The plan it
    shows is as of the last checkpoint, not live PR state.
    """
    try:
        report = _gather_status(opts)
    except (OSError, ValueError, KeyError) as e:
        message = f"Failed to load state file {_state_path(opts)}: {e}"
        if output == "json":
            # Keep stdout parseable: it carries JSON or nothing.
            print(message, file=sys.stderr)
        else:
            console.print(f"[red]{_escape_markup(message)}[/red]")
        sys.exit(1)

    if output == "json":
        # Plain print, not the console: rich would wrap and highlight it.
        print(json.dumps(_status_json(report), indent=2))
    else:
        _print_status_text(report)
