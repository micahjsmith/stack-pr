"""The ``autoland --status`` report."""

from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from stack_pr import git
from stack_pr.autoland import runtime
from stack_pr.autoland.display import (
    _escape_markup,
    _land_entry,
    _plan_rows,
    _StepRow,
    print_status,
)
from stack_pr.autoland.model import (
    _STEP_TYPES,
    LandingContext,
    LandStep,
    PlanStep,
    WorkflowStep,
)
from stack_pr.autoland.options import AutolandOptions
from stack_pr.autoland.state import AutolandCheckpointer, AutolandLock, _state_path

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
    branch = opts.branch or ("" if opts.state_file else git.get_current_branch_name())

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
        runtime.console.print(f"No autoland in progress for {target}.")
        runtime.console.print(
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

        runtime.console.print("[bold]Autoland status[/bold]\n")
        width = max(len(name) for name, _ in fields)
        for name, value in fields:
            runtime.console.print(
                f"  [dim]{name + ':':<{width + 1}}[/dim] {value}", soft_wrap=True
            )
        if report.ctx is not None:
            runtime.console.print()
            print_status(report.ctx)
        if report.resume_command:
            runtime.console.print(
                f"\n[dim]Resume with: {report.resume_command}[/dim]", soft_wrap=True
            )

    if report.others:
        runtime.console.print("\n[bold]Other autolands with saved state:[/bold]")
        for other in report.others:
            branch = other.branch or "(unreadable state file)"
            label = (
                "[cyan]in progress[/cyan]"
                if other.running
                else "[yellow]stopped[/yellow]"
            )
            runtime.console.print(
                f"  {_escape_markup(branch)} — {label} — "
                f"[dim]{_escape_markup(str(other.state_path))}[/dim]",
                soft_wrap=True,
            )
        runtime.console.print(
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
            runtime.console.print(f"[red]{_escape_markup(message)}[/red]")
        sys.exit(1)

    if output == "json":
        # Plain print, not the console: rich would wrap and highlight it.
        print(json.dumps(_status_json(report), indent=2))
    else:
        _print_status_text(report)
