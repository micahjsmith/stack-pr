# stack-pr autoland: land a whole stack through the GitHub merge queue.
#
# This module holds the full autoland engine. cli.py only wires up the
# subparser and dispatches into `run_autoland` to keep cli.py small.
#
# Repo-specific behavior (which CI checks gate a merge, poll intervals,
# retry counts, deploy timeouts, and whether the repo uses a merge queue at
# all) is externalized to the `[autoland]` config section and command-line
# flags, so a repo can reproduce its workflow with configuration alone.
from __future__ import annotations

import argparse
import configparser
import functools
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
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
DEFAULT_DEPLOY_TIMEOUT = 10800  # 3 hours
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
    from rich.table import Table
    from rich.text import Text

    HAVE_RICH = True
except ImportError:  # pragma: no cover - exercised only without the extra
    Console = _PlainConsole  # type: ignore[assignment,misc]
    Table = None  # type: ignore[assignment,misc]
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
    deploy_timeout: int
    dry_run: bool
    branch: str | None
    interactive: bool
    resume: bool
    state_file: Path | None
    always_cleanup: bool

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
        return cls(
            merge_queue=config.getboolean(
                "autoland", "merge_queue", fallback=False
            ),
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
            deploy_timeout=_int(
                getattr(args, "deploy_timeout", None),
                "deploy_timeout",
                DEFAULT_DEPLOY_TIMEOUT,
            ),
            dry_run=bool(getattr(args, "dry_run", False)),
            branch=getattr(args, "branch", None),
            interactive=bool(getattr(args, "interactive", False)),
            resume=bool(getattr(args, "resume", False)),
            state_file=Path(state_file) if state_file else None,
            always_cleanup=bool(getattr(args, "always_cleanup", False)),
        )


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


class PRState(str, Enum):
    PENDING = "pending"
    WAITING_FOR_APPROVAL = "waiting_for_approval"
    WAITING_FOR_CHECKS = "waiting_for_checks"
    IN_MERGE_QUEUE = "in_merge_queue"
    WAITING_FOR_DEPLOY = "waiting_for_deploy"
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
    """Land the next PR in the stack through the merge queue."""

    entry_index: int


@dataclass
class DeployStep:
    """Wait for a deploy workflow to succeed with the landed code."""

    workflow: str
    state: str = "pending"  # pending, waiting, succeeded, failed
    error_message: str = ""


@dataclass
class ConfirmStep:
    """Pause until the user types ``y``/``Y`` and presses Enter to continue."""

    message: str = "Confirm to continue"
    confirmed: bool = False


PlanStep = Union[LandStep, DeployStep, ConfirmStep]


@dataclass
class LandingContext:
    """Mutable state for the landing run."""

    stack: list[StackEntry] = field(default_factory=list)
    plan: list[PlanStep] = field(default_factory=list)
    current_step: int = 0
    current_index: int = 0  # index into stack for the active land step
    aborted: bool = False
    abort_reason: str = ""
    last_landed_sha: str = ""  # origin/<target> HEAD after the last merge


# ---------------------------------------------------------------------------
# State persistence (checkpoint / resume)
# ---------------------------------------------------------------------------

STATE_VERSION = 1

_STEP_TYPES = {LandStep: "land", DeployStep: "deploy", ConfirmStep: "confirm"}


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
    if data["type"] == "deploy":
        return DeployStep(**fields)
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


@functools.lru_cache(maxsize=1)
def get_owner_repo() -> tuple[str, str]:
    """Return ``(owner, repo)`` for the current repository via gh."""
    data = gh_json(["repo", "view", "--json", "owner,name"])
    assert isinstance(data, dict)
    return data["owner"]["login"], data["name"]


# ---------------------------------------------------------------------------
# Worktree management
# ---------------------------------------------------------------------------

_worktree_path: Path | None = None
_orig_cwd: str | None = None


def setup_worktree(branch: str) -> str:
    """Create a temp worktree for *branch* and chdir into it."""
    global _worktree_path, _orig_cwd

    tmpdir = tempfile.mkdtemp(prefix="autoland-")
    worktree_dir = str(Path(tmpdir) / "repo")

    console.print(
        f"[bold]Creating temporary worktree for [cyan]{branch}[/cyan] "
        f"at {worktree_dir}[/bold]"
    )
    subprocess.run(
        ["git", "worktree", "add", "-f", worktree_dir, branch],
        check=True,
        capture_output=True,
        text=True,
    )

    _worktree_path = Path(worktree_dir)
    _orig_cwd = str(Path.cwd())
    os.chdir(worktree_dir)
    return worktree_dir


def cleanup_worktree() -> None:
    global _worktree_path, _orig_cwd
    if _worktree_path is None:
        return

    wt = _worktree_path
    _worktree_path = None
    if _orig_cwd:
        os.chdir(_orig_cwd)
        _orig_cwd = None

    console.print(f"\n[dim]Cleaning up worktree at {wt}...[/dim]")
    subprocess.run(
        ["git", "worktree", "remove", "--force", str(wt)],
        check=False,
        capture_output=True,
        text=True,
    )
    shutil.rmtree(wt.parent, ignore_errors=True)


def print_worktree_path() -> None:
    if _worktree_path is not None:
        console.print(
            f"\n[bold yellow]Worktree preserved at: "
            f"[cyan]{_worktree_path}[/cyan][/bold yellow]"
        )
        console.print(
            "[dim]To clean up manually: "
            f"git worktree remove --force {_worktree_path}[/dim]"
        )


# ---------------------------------------------------------------------------
# Stack discovery (reuses stack-pr's get_stack)
# ---------------------------------------------------------------------------


def discover_stack(common: cli.CommonArgs) -> list[StackEntry]:
    """Discover the stack via stack-pr's own parser, bottom-to-top order."""
    entries: list[StackEntry] = []
    for e in cli.get_stack(
        base=common.base, head=common.head, verbose=common.verbose
    ):
        if not e.has_pr():
            continue  # commit not submitted yet — skip
        pr_number = int(cli.last(e.pr))
        entries.append(
            StackEntry(pr_url=e.pr, pr_number=pr_number, branch=e.head)
        )
    return entries


def enrich_stack(stack: list[StackEntry]) -> None:
    """Fetch PR titles, review status, and current state from GitHub."""
    for entry in stack:
        try:
            data = gh_json(
                [
                    "pr",
                    "view",
                    str(entry.pr_number),
                    "--json",
                    "title,state,reviewDecision",
                ]
            )
            assert isinstance(data, dict)
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


def get_check_runs(pr_number: int) -> list[dict]:
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


def evaluate_checks(pr_number: int, required_checks: list[str]) -> CheckResult:
    """Evaluate checks for a PR.

    If *required_checks* is non-empty, gate on exactly those named checks.
    Otherwise gate on all reported checks that aren't being skipped.
    """
    checks = get_check_runs(pr_number)

    if required_checks:
        check_map = {
            c.get("name", ""): c
            for c in checks
            if c.get("name", "") in required_checks
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


def rerun_failed_jobs(run_ids: list[int]) -> None:
    seen: set[int] = set()
    for run_id in run_ids:
        if run_id in seen:
            continue
        seen.add(run_id)
        try:
            run(["gh", "run", "rerun", str(run_id), "--failed"], quiet=False)
        except RuntimeError as e:
            console.print(f"[yellow]Warning: could not rerun {run_id}: {e}[/yellow]")


# ---------------------------------------------------------------------------
# Merge queue interaction
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


def has_merge_queue_entry(pr_number: int) -> bool:
    """Whether a PR currently has an active merge queue entry (GraphQL)."""
    try:
        owner, repo = get_owner_repo()
        result = run(
            [
                "gh", "api", "graphql",
                "-F", f"owner={owner}",
                "-F", f"repo={repo}",
                "-F", f"number={pr_number}",
                "-f", f"query={_MERGE_QUEUE_ENTRY_QUERY}",
            ],
            quiet=True,
        )
        data = json.loads(result.stdout)
        entry = (
            data.get("data", {})
            .get("repository", {})
            .get("pullRequest", {})
            .get("mergeQueueEntry")
        )
        return entry is not None
    except (RuntimeError, json.JSONDecodeError):
        return False


def add_to_merge_queue(pr_number: int) -> None:
    run(["gh", "pr", "merge", str(pr_number), "--squash"], quiet=False)


@dataclass
class MergeQueuePollResult:
    merged: bool = False
    booted: bool = False
    error: str = ""


def poll_merge_status(pr_number: int) -> MergeQueuePollResult:
    data = gh_json(["pr", "view", str(pr_number), "--json", "state"])
    assert isinstance(data, dict)
    state = data.get("state", "")

    if state == "MERGED":
        return MergeQueuePollResult(merged=True)
    if state == "CLOSED":
        return MergeQueuePollResult(error="PR was closed")
    if state == "OPEN":
        if has_merge_queue_entry(pr_number):
            return MergeQueuePollResult()  # still in queue
        return MergeQueuePollResult(booted=True)
    return MergeQueuePollResult()


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

    console.print("[bold]Re-submitting stack...[/bold]")
    cli.command_submit(
        common, draft=False, reviewer="", keep_body=True, draft_bitmask=None
    )


# ---------------------------------------------------------------------------
# Deploy checkpoint polling
# ---------------------------------------------------------------------------


def _refresh_last_landed_sha(ctx: LandingContext, common: cli.CommonArgs) -> None:
    try:
        run(["git", "fetch", common.remote, common.target], quiet=True)
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


def wait_for_deploy(
    step: DeployStep,
    *,
    target_sha: str,
    deploy_timeout: int,
    poll_interval: int,
    target: str,
    ctx: LandingContext,
) -> bool:
    """Wait for a deploy workflow to complete with code at or after target_sha."""
    step.state = "waiting"
    console.print(
        f"\n[bold blue]Waiting for deploy: {step.workflow}[/bold blue]"
        f"\n[dim]Target SHA: {target_sha[:12]}[/dim]"
    )

    awake_elapsed = 0.0
    while True:
        if ctx.aborted:
            return False
        if awake_elapsed > deploy_timeout:
            step.state = "failed"
            step.error_message = f"Deploy timed out after {deploy_timeout / 3600:.0f}h"
            return False

        try:
            data = gh_json(
                [
                    "run", "list",
                    "--workflow", step.workflow,
                    "--branch", target,
                    "--json", "headSha,status,conclusion",
                    "--limit", "10",
                ]
            )
        except RuntimeError as e:
            console.print(f"[yellow]Warning: could not poll deploy: {e}[/yellow]")
            resilient_sleep(poll_interval)
            awake_elapsed += poll_interval
            continue

        assert isinstance(data, list)
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
                    f"\n[bold green]Deploy {step.workflow} completed "
                    f"with SHA {run_sha[:12]}[/bold green]"
                )
                return True

        mins = int(awake_elapsed) // 60
        step.error_message = f"Waiting for deploy ({mins}m elapsed)..."
        console.print(
            f"[dim]Deploy {step.workflow}: waiting ({mins}m) — "
            f"polling in {poll_interval}s[/dim]"
        )
        resilient_sleep(poll_interval)
        awake_elapsed += poll_interval


# ---------------------------------------------------------------------------
# Interactive plan editing
# ---------------------------------------------------------------------------


def generate_default_plan(stack: list[StackEntry]) -> list[PlanStep]:
    return [LandStep(entry_index=i) for i in range(len(stack))]


def format_plan_for_editor(stack: list[StackEntry], plan: list[PlanStep]) -> str:
    lines = [
        "# Autoland plan — edit steps below.",
        "# l             = land the next PR in the stack",
        "# d <workflow>  = wait for deploy workflow to complete",
        "# c <message>   = pause and wait for manual confirmation",
        "#",
        "# Lines starting with # are comments and are ignored.",
        "# Blank lines are ignored.",
        "#",
    ]
    for step in plan:
        if isinstance(step, LandStep):
            entry = stack[step.entry_index]
            lines.append(f"l    # PR #{entry.pr_number}: {entry.title}")
        elif isinstance(step, DeployStep):
            lines.append(f"d {step.workflow}")
        elif isinstance(step, ConfirmStep):
            lines.append(f"c {step.message}")
    lines.append("")
    return "\n".join(lines)


def parse_plan(text: str, stack: list[StackEntry]) -> list[PlanStep]:
    """Parse an edited plan back into steps. Raises ValueError if malformed."""
    steps: list[PlanStep] = []
    land_counter = 0

    for line_num, raw_line in enumerate(text.splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if " #" in line:
            line = line[: line.index(" #")].strip()

        if line == "l":
            if land_counter >= len(stack):
                raise ValueError(
                    f"Line {line_num}: too many 'l' steps — "
                    f"only {len(stack)} PRs in stack"
                )
            steps.append(LandStep(entry_index=land_counter))
            land_counter += 1
        elif line.startswith("d "):
            workflow = line[2:].strip()
            if not workflow:
                raise ValueError(
                    f"Line {line_num}: 'd' requires a workflow name"
                )
            steps.append(DeployStep(workflow=workflow))
        elif line == "c" or line.startswith("c "):
            message = (
                line[2:].strip() if line.startswith("c ") else "Confirm to continue"
            )
            steps.append(ConfirmStep(message=message))
        else:
            raise ValueError(f"Line {line_num}: unrecognized step: {raw_line!r}")

    if land_counter != len(stack):
        raise ValueError(
            f"Plan has {land_counter} land steps but stack has "
            f"{len(stack)} PRs — all PRs must be landed"
        )
    return steps


def edit_plan_interactive(stack: list[StackEntry]) -> list[PlanStep]:
    """Open the default plan in $EDITOR and return the parsed result."""
    plan_text = format_plan_for_editor(stack, generate_default_plan(stack))
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
    PRState.WAITING_FOR_DEPLOY: "Waiting for deploy",
    PRState.MERGED: "Merged",
    PRState.FAILED: "Failed",
}

STATE_STYLES = {
    PRState.PENDING: "dim",
    PRState.WAITING_FOR_APPROVAL: "magenta",
    PRState.WAITING_FOR_CHECKS: "yellow",
    PRState.IN_MERGE_QUEUE: "cyan",
    PRState.WAITING_FOR_DEPLOY: "blue",
    PRState.MERGED: "green",
    PRState.FAILED: "red bold",
}

_REVIEW_DECISION_DISPLAY = {
    "APPROVED": ("Approved", "green"),
    "REVIEW_REQUIRED": ("Review required", "magenta"),
    "CHANGES_REQUESTED": ("Changes requested", "red"),
}

_DEPLOY_STEP_STYLES = {
    "pending": ("Pending", "dim"),
    "waiting": ("Waiting for deploy", "blue"),
    "succeeded": ("Deploy complete", "green"),
    "failed": ("Failed", "red bold"),
}


def render_status_table(ctx: LandingContext) -> Table:  # pragma: no cover
    """Render plan progress as a rich table (rich-only path)."""
    table = Table(title="Autoland Progress", show_header=False, show_lines=True)
    table.add_column("Field", style="bold dim", width=14, no_wrap=True)
    table.add_column("Value", min_width=40)

    plan = ctx.plan or generate_default_plan(ctx.stack)
    for step_idx, step in enumerate(plan):
        if step_idx > 0:
            table.add_section()
        pointer = "->" if step_idx == ctx.current_step else " "

        if isinstance(step, LandStep):
            entry = ctx.stack[step.entry_index]
            table.add_row(
                f"{pointer} Step {step_idx + 1}/{len(plan)}",
                Text(f"Land PR #{entry.pr_number}", style="bold"),
            )
            table.add_row("  Title", entry.title or "(untitled)")
            status_text = STATE_LABELS.get(entry.state, str(entry.state))
            if entry.error_message:
                status_text += f"\n  {entry.error_message}"
            table.add_row(
                "  Status",
                Text(status_text, style=STATE_STYLES.get(entry.state, "")),
            )
            table.add_row(
                "  Retries",
                f"CI {entry.check_retries} - MQ {entry.queue_retries}",
            )
        elif isinstance(step, DeployStep):
            table.add_row(
                f"{pointer} Step {step_idx + 1}/{len(plan)}",
                Text("Deploy checkpoint", style="bold blue"),
            )
            table.add_row("  Workflow", step.workflow)
            label, ds_style = _DEPLOY_STEP_STYLES.get(step.state, ("Pending", "dim"))
            table.add_row("  Status", Text(label, style=ds_style))
        elif isinstance(step, ConfirmStep):
            table.add_row(
                f"{pointer} Step {step_idx + 1}/{len(plan)}",
                Text("Manual confirmation", style="bold yellow"),
            )
            table.add_row("  Message", step.message)

    if ctx.aborted:
        table.caption = f"ABORTED: {ctx.abort_reason}"
    return table


def render_status_plain(ctx: LandingContext) -> str:
    lines = ["", "Autoland Progress", "================="]
    plan = ctx.plan or generate_default_plan(ctx.stack)
    for i, step in enumerate(plan):
        cur = "->" if i == ctx.current_step else "  "
        if isinstance(step, LandStep):
            e = ctx.stack[step.entry_index]
            lines.append(
                f"{cur} [{i + 1}/{len(plan)}] Land PR #{e.pr_number}: "
                f"{e.title or '(untitled)'}"
            )
            status = STATE_LABELS.get(e.state, str(e.state))
            if e.error_message:
                status += f" — {e.error_message}"
            lines.append(f"      status: {status}")
            lines.append(
                f"      retries: CI {e.check_retries} / MQ {e.queue_retries}"
            )
        elif isinstance(step, DeployStep):
            label, _ = _DEPLOY_STEP_STYLES.get(step.state, ("Pending", ""))
            lines.append(
                f"{cur} [{i + 1}/{len(plan)}] Deploy: {step.workflow} — {label}"
            )
        elif isinstance(step, ConfirmStep):
            state = (
                "confirmed"
                if step.confirmed
                else ("waiting" if i == ctx.current_step else "pending")
            )
            lines.append(
                f"{cur} [{i + 1}/{len(plan)}] Confirm: {step.message} — {state}"
            )
    if ctx.aborted:
        lines.append(f"ABORTED: {ctx.abort_reason}")
    return "\n".join(lines)


def print_status(ctx: LandingContext) -> None:
    if HAVE_RICH:
        console.print(render_status_table(ctx))
    else:
        console.print(render_status_plain(ctx))


# ---------------------------------------------------------------------------
# Landing logic
# ---------------------------------------------------------------------------


def check_pr_state(pr_number: int) -> str:
    data = gh_json(["pr", "view", str(pr_number), "--json", "state"])
    assert isinstance(data, dict)
    return data.get("state", "OPEN")


def refresh_review_decision(entry: StackEntry) -> None:
    try:
        data = gh_json(["pr", "view", str(entry.pr_number), "--json", "reviewDecision"])
        assert isinstance(data, dict)
        entry.review_decision = data.get("reviewDecision", "")
    except RuntimeError:
        pass


def wait_for_approval(
    entry: StackEntry, *, poll_interval: int, ctx: LandingContext
) -> bool:
    """Wait until the PR has required approvals. Returns False if aborted."""
    pr_state = check_pr_state(entry.pr_number)
    if pr_state == "MERGED":
        entry.state = PRState.MERGED
        return True
    if pr_state == "CLOSED":
        entry.state = PRState.FAILED
        entry.error_message = "PR was closed"
        return False

    refresh_review_decision(entry)
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
            f"polling in {poll_interval}s[/dim]"
        )
        resilient_sleep(poll_interval)

        pr_state = check_pr_state(entry.pr_number)
        if pr_state == "MERGED":
            entry.state = PRState.MERGED
            return True
        if pr_state == "CLOSED":
            entry.state = PRState.FAILED
            entry.error_message = "PR was closed"
            return False

        refresh_review_decision(entry)
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
    entry: StackEntry,
    *,
    required_checks: list[str],
    max_retries: int,
    poll_interval: int,
    ctx: LandingContext,
) -> bool:
    """Wait for required checks to pass. Returns True on success."""
    entry.state = PRState.WAITING_FOR_CHECKS

    while True:
        if ctx.aborted:
            return False

        pr_state = check_pr_state(entry.pr_number)
        if pr_state == "MERGED":
            entry.state = PRState.MERGED
            return True
        if pr_state == "CLOSED":
            entry.state = PRState.FAILED
            entry.error_message = "PR was closed"
            return False

        result = evaluate_checks(entry.pr_number, required_checks)
        entry.error_message = result.summary

        if result.status == CheckStatus.ALL_PASSING:
            entry.error_message = "All checks passing"
            return True

        if result.status == CheckStatus.FAILED:
            if entry.check_retries >= max_retries:
                entry.state = PRState.FAILED
                entry.error_message = (
                    f"Checks failed after {max_retries} retries: "
                    f"{', '.join(result.failed_names)}"
                )
                return False
            entry.check_retries += 1
            console.print(
                f"[yellow]Rerunning failed checks "
                f"(attempt {entry.check_retries}/{max_retries}): "
                f"{', '.join(result.failed_names)}[/yellow]"
            )
            rerun_failed_jobs(result.failed_runs)

        console.print(
            f"[dim]PR #{entry.pr_number}: {result.summary} — "
            f"polling in {poll_interval}s[/dim]"
        )
        resilient_sleep(poll_interval)


_MERGEABLE_STATES = {"CLEAN", "UNSTABLE", "HAS_HOOKS"}


def get_merge_state(pr_number: int) -> dict:
    data = gh_json(
        ["pr", "view", str(pr_number), "--json", "state,mergeStateStatus,mergeable"]
    )
    assert isinstance(data, dict)
    return data


@dataclass
class MergeableResult:
    ready: bool = False
    already_merged: bool = False
    error: str = ""


def wait_for_mergeable(
    entry: StackEntry, *, poll_interval: int, ctx: LandingContext
) -> MergeableResult:
    """Wait until GitHub reports the PR as mergeable."""
    while True:
        if ctx.aborted:
            return MergeableResult(error="aborted")

        data = get_merge_state(entry.pr_number)
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
            resilient_sleep(poll_interval)
            continue

        # UNKNOWN can also mean "already in the merge queue".
        if merge_state == "UNKNOWN" and has_merge_queue_entry(entry.pr_number):
            console.print(
                f"[cyan]PR #{entry.pr_number} is already in the merge queue — "
                "skipping enqueue[/cyan]"
            )
            return MergeableResult(ready=True, already_merged=False)

        entry.error_message = f"Waiting for mergeable state (currently {merge_state})"
        console.print(
            f"[dim]PR #{entry.pr_number}: mergeStateStatus={merge_state} — "
            f"polling in {poll_interval}s[/dim]"
        )
        resilient_sleep(poll_interval)


def enqueue_and_wait(
    entry: StackEntry,
    *,
    required_checks: list[str],
    max_queue_retries: int,
    max_check_retries: int,
    poll_interval: int,
    merge_timeout: int,
    ctx: LandingContext,
) -> bool:
    """Add the PR to the merge queue and wait for it to merge."""
    while True:
        if ctx.aborted:
            return False

        mergeable_result = wait_for_mergeable(
            entry, poll_interval=poll_interval, ctx=ctx
        )
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
            add_to_merge_queue(entry.pr_number)
        except RuntimeError as e:
            entry.error_message = f"Failed to enqueue: {e}"
            console.print(f"[red]Failed to add to merge queue: {e}[/red]")
            if entry.queue_retries >= max_queue_retries:
                entry.state = PRState.FAILED
                entry.error_message = (
                    f"Failed to enqueue after {max_queue_retries} attempts"
                )
                return False
            entry.queue_retries += 1
            if not wait_for_approval(entry, poll_interval=poll_interval, ctx=ctx):
                return False
            entry.check_retries = 0
            if not wait_for_checks(
                entry,
                required_checks=required_checks,
                max_retries=max_check_retries,
                poll_interval=poll_interval,
                ctx=ctx,
            ):
                return False
            continue

        entry.error_message = "Waiting in merge queue..."
        awake_elapsed = 0.0
        while True:
            if ctx.aborted:
                return False
            if awake_elapsed > merge_timeout:
                entry.state = PRState.FAILED
                entry.error_message = "Timed out waiting for merge queue"
                return False

            poll = poll_merge_status(entry.pr_number)
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
                if entry.queue_retries >= max_queue_retries:
                    entry.state = PRState.FAILED
                    entry.error_message = (
                        f"Booted from queue {max_queue_retries} times, giving up"
                    )
                    return False
                entry.queue_retries += 1
                if not wait_for_approval(entry, poll_interval=poll_interval, ctx=ctx):
                    return False
                entry.check_retries = 0
                if not wait_for_checks(
                    entry,
                    required_checks=required_checks,
                    max_retries=max_check_retries,
                    poll_interval=poll_interval,
                    ctx=ctx,
                ):
                    return False
                break  # re-enqueue in outer loop

            mins = int(awake_elapsed) // 60
            entry.error_message = f"In merge queue ({mins}m elapsed)..."
            console.print(
                f"[dim]PR #{entry.pr_number}: in merge queue ({mins}m) — "
                f"polling in {poll_interval}s[/dim]"
            )
            resilient_sleep(poll_interval)
            awake_elapsed += poll_interval


def execute_plan(
    ctx: LandingContext,
    common: cli.CommonArgs,
    opts: AutolandOptions,
    checkpointer: AutolandCheckpointer,
) -> bool:
    """Execute the landing plan from ctx.current_step. Returns True on success."""
    for step_idx in range(ctx.current_step, len(ctx.plan)):
        step = ctx.plan[step_idx]
        ctx.current_step = step_idx
        checkpointer.save(ctx)

        if isinstance(step, LandStep):
            entry = ctx.stack[step.entry_index]
            ctx.current_index = step.entry_index

            if entry.state == PRState.MERGED:
                console.print(
                    f"\n[green]PR #{entry.pr_number} already merged, skipping[/green]"
                )
                _refresh_last_landed_sha(ctx, common)
                continue

            console.print(
                f"\n{'=' * 60}\n[bold]Step {step_idx + 1}/{len(ctx.plan)}: "
                f"Landing PR #{entry.pr_number} — {entry.title}[/bold]\n{'=' * 60}"
            )

            if not wait_for_approval(entry, poll_interval=opts.poll_interval, ctx=ctx):
                return _abort(ctx, checkpointer, f"PR #{entry.pr_number} approval wait was aborted")

            if entry.state == PRState.MERGED:
                _refresh_last_landed_sha(ctx, common)
            else:
                if not wait_for_checks(
                    entry,
                    required_checks=opts.required_checks,
                    max_retries=opts.max_check_retries,
                    poll_interval=opts.poll_interval,
                    ctx=ctx,
                ):
                    return _abort(
                        ctx, f"PR #{entry.pr_number} checks failed after retries"
                    )

                if entry.state == PRState.MERGED:
                    _refresh_last_landed_sha(ctx, common)
                else:
                    if not enqueue_and_wait(
                        entry,
                        required_checks=opts.required_checks,
                        max_queue_retries=opts.max_queue_retries,
                        max_check_retries=opts.max_check_retries,
                        poll_interval=opts.poll_interval,
                        merge_timeout=opts.merge_timeout,
                        ctx=ctx,
                    ):
                        return _abort(ctx, checkpointer, f"PR #{entry.pr_number} failed to merge")
                    _refresh_last_landed_sha(ctx, common)

            has_more_land_steps = any(
                isinstance(s, LandStep) for s in ctx.plan[step_idx + 1 :]
            )
            if has_more_land_steps:
                try:
                    rebase_and_resubmit(common)
                except Exception as e:  # noqa: BLE001 - report any resubmit failure
                    return _abort(
                        ctx, f"Rebase failed after merging #{entry.pr_number}: {e}"
                    )

        elif isinstance(step, DeployStep):
            console.print(
                f"\n{'=' * 60}\n[bold]Step {step_idx + 1}/{len(ctx.plan)}: "
                f"Deploy checkpoint — {step.workflow}[/bold]\n{'=' * 60}"
            )
            if not ctx.last_landed_sha:
                _refresh_last_landed_sha(ctx, common)
            if not wait_for_deploy(
                step,
                target_sha=ctx.last_landed_sha,
                deploy_timeout=opts.deploy_timeout,
                poll_interval=opts.poll_interval,
                target=common.target,
                ctx=ctx,
            ):
                return _abort(ctx, checkpointer, f"Deploy {step.workflow} failed or timed out")

        elif isinstance(step, ConfirmStep):
            if step.confirmed:
                continue
            console.print(
                f"\n{'=' * 60}\n[bold yellow]Step {step_idx + 1}/{len(ctx.plan)}: "
                f"Manual confirmation required[/bold yellow]\n{'=' * 60}\n"
                f"\n[bold]{step.message}[/bold]\n"
            )
            while True:
                try:
                    answer = console.input(
                        "[yellow]Type y/Y then Enter to continue "
                        "(Ctrl+C to abort): [/yellow]"
                    ).strip()
                except EOFError:
                    return _abort(
                        ctx,
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
        "--deploy-timeout",
        type=int,
        default=None,
        help="Seconds to wait for a deploy checkpoint (config: autoland.deploy_timeout).",
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
    p.add_argument(
        "-i",
        "--interactive",
        action="store_true",
        help="Edit the landing plan in $EDITOR (add deploy/confirm checkpoints).",
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

    if opts.resume:
        _run_resume(common, opts)
        return
    _run_fresh(common, opts)


def _install_signal_handler(
    ctx: LandingContext, checkpointer: AutolandCheckpointer, opts: AutolandOptions
) -> None:
    def handler(_sig: int, _frame: object) -> None:
        ctx.aborted = True
        ctx.abort_reason = "User interrupted (Ctrl+C)"
        checkpointer.save(ctx)
        console.print(
            "\n[red bold]Interrupted! State saved. Resume with --resume.[/red bold]\n"
        )
        print_status(ctx)
        if opts.always_cleanup:
            cleanup_worktree()
        else:
            print_worktree_path()
        sys.exit(130)

    signal.signal(signal.SIGINT, handler)


def _finish(
    ctx: LandingContext,
    checkpointer: AutolandCheckpointer,
    opts: AutolandOptions,
    *,
    success: bool,
) -> None:
    console.print("\n")
    print_status(ctx)
    if success:
        console.print("\n[bold green]All PRs landed successfully![/bold green]\n")
        checkpointer.delete()
        cleanup_worktree()
    else:
        console.print(f"\n[bold red]Landing failed: {ctx.abort_reason}[/bold red]\n")
        console.print(
            f"[dim]State saved to {checkpointer.path} — resume with --resume[/dim]\n"
        )
        if opts.always_cleanup:
            cleanup_worktree()
        else:
            print_worktree_path()
        sys.exit(1)


def _run_fresh(common: cli.CommonArgs, opts: AutolandOptions) -> None:
    if opts.branch:
        setup_worktree(opts.branch)
        console.print(
            f"[green]Working in temporary worktree for [bold]{opts.branch}"
            "[/bold][/green]\n"
        )

    console.print("\n[bold]Discovering stack...[/bold]\n")
    stack = discover_stack(common)
    if not stack:
        console.print("[red]No stack found on the current branch.[/red]")
        sys.exit(1)
    enrich_stack(stack)

    plan = (
        edit_plan_interactive(stack)
        if opts.interactive
        else generate_default_plan(stack)
    )
    ctx = LandingContext(stack=stack, plan=plan)

    branch = opts.branch or _current_branch()
    checkpointer = AutolandCheckpointer(
        path=opts.state_file or AutolandCheckpointer.default_path(branch),
        branch=branch,
        base=common.target,
    )

    print_status(ctx)
    if opts.dry_run:
        console.print("\n[yellow]Dry run — exiting.[/yellow]")
        return

    console.print(f"[dim]State file: {checkpointer.path}[/dim]\n")
    _install_signal_handler(ctx, checkpointer, opts)
    _finish(ctx, checkpointer, opts, success=execute_plan(ctx, common, opts, checkpointer))


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

    console.print(f"[bold]Resuming from checkpoint: [cyan]{sf_path}[/cyan][/bold]\n")
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
        setup_worktree(opts.branch or checkpointer.branch)

    console.print("[dim]Refreshing PR state from GitHub...[/dim]")
    enrich_stack(ctx.stack)
    ctx.aborted = False
    ctx.abort_reason = ""

    if ctx.current_step >= len(ctx.plan):
        console.print("[green]All steps already completed — nothing to resume.[/green]")
        checkpointer.delete()
        return

    print_status(ctx)
    console.print(f"[dim]State file: {sf_path}[/dim]\n")
    _install_signal_handler(ctx, checkpointer, opts)
    _finish(ctx, checkpointer, opts, success=execute_plan(ctx, common, opts, checkpointer))
