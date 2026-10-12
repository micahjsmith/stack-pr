"""The tools a plan step may use while it executes, injected through ``Services``.

A step, an execution unit, and everything they call while landing (the wait
loops, the stack merge, the landed-SHA lookup, rebase + resubmit, an ``x``
step's command) reach the console, GitHub, the clock, the local repo, and
subprocesses only through one ``Services``, never through module globals, so
any of them can be run against fakes.

``Services.production()`` binds the real implementations: the ``gh.github``
client, the ``runtime.console``, ``runtime.resilient_sleep``, a ``GitRepo``,
``SubprocessCommands``, and the polling waits of ``autoland.waits``.

Module singletons remain only for what runs outside a plan's execution: the
command flows' own output and prompts (``commands``), ``--status``, plan
parsing and the discovery of the stack (which ask ``gh.github``), and the
shell runner (``runtime.run``), which echoes each command it runs to
``runtime.console``.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from stack_pr import shell_commands
from stack_pr.autoland import display, gh, runtime, waits
from stack_pr.autoland.repo import GitRepo

if TYPE_CHECKING:
    from stack_pr import cli
    from stack_pr.autoland.model import LandingContext, StackEntry, WorkflowStep
    from stack_pr.autoland.options import AutolandOptions
    from stack_pr.autoland.waits import MergeableResult


class UI(Protocol):
    """Where a step talks to the user."""

    def print(self, *objects: object) -> None:
        """Show console markup (``[bold]...[/bold]``) to the user."""

    def input(self, prompt: str) -> str:
        """Ask the user for a line. Raises EOFError when there is no input."""

    def escape(self, text: str) -> str:
        """*text* made safe to embed in markup, so it shows literally."""


class Waits(Protocol):
    """The long-running waits of landing a PR or checking a workflow.

    Each returns whether it succeeded, and records its progress on the entry
    or step it is given. Ctrl+C while one is waiting raises KeyboardInterrupt
    out of it, which the run handles at its boundary (``commands._land``).

    The production waits (``PollingWaits``) poll GitHub through the other
    services. This is a service of its own so that a step, or a stack merge,
    can be tested against waits that just succeed or fail.
    """

    def wait_for_approval(
        self, entry: StackEntry, *, opts: AutolandOptions
    ) -> bool: ...

    def wait_for_checks(self, entry: StackEntry, *, opts: AutolandOptions) -> bool: ...

    def wait_for_mergeable(
        self, entry: StackEntry, *, opts: AutolandOptions
    ) -> MergeableResult: ...

    def enqueue_and_wait(self, entry: StackEntry, *, opts: AutolandOptions) -> bool: ...

    def wait_for_workflow(
        self,
        step: WorkflowStep,
        target_sha: str,
        *,
        opts: AutolandOptions,
        common: cli.CommonArgs,
        ctx: LandingContext,
    ) -> bool:
        """Wait for a successful run of the step's workflow at or after
        *target_sha*."""


class Repo(Protocol):
    """The local clone and the stack-pr operations on it."""

    def fetch_target(self, common: cli.CommonArgs) -> None:
        """Fetch the target branch. Best-effort: never raises."""

    def is_ancestor(self, ancestor: str, descendant: str) -> bool | None:
        """Whether *ancestor* is an ancestor of (or is) *descendant*, per this
        clone. ``None`` if the clone can't tell (e.g. a commit is missing)."""

    def landed_sha(self, common: cli.CommonArgs, pr_number: int | None = None) -> str:
        """The commit of the code landed so far: *pr_number*'s merge commit,
        or else the head of the target branch. "" if it can't be found."""

    def rebase_and_resubmit(self, common: cli.CommonArgs) -> None:
        """Rebase the local stack onto the target and re-submit it. Raises on
        failure."""

    def root(self) -> Path:
        """The top-level directory of the checkout being landed (the temporary
        worktree, with ``--branch``)."""


class Commands(Protocol):
    """Running a user's command, as an ``x`` step does.

    A service of its own, rather than part of ``Repo``, because what it runs
    is the user's program, not an operation on the clone; and so a step can
    be tested against commands that just exit with a given status.
    """

    def run(self, argv: list[str], *, cwd: Path, env: Mapping[str, str]) -> int:
        """Run *argv* (no shell) in *cwd*, with *env* set on top of autoland's
        own environment, its output going straight to the terminal. Returns
        its exit status: negative if a signal killed it. Raises OSError if it
        can't be started (e.g. no such program). Ctrl+C raises
        KeyboardInterrupt out of it once the command has been stopped."""


@dataclass(frozen=True)
class Services:
    """Everything a step may use beyond its ``StepContext``'s run state.

    ``waits`` defaults to the real polling waits over these services' GitHub,
    UI, sleep, and repo. They always use the ``Services`` they belong to, so
    ``dataclasses.replace`` of, say, ``github`` gives waits that poll the new
    one.
    """

    github: gh.GitHub
    ui: UI
    # Sleep for some seconds, surviving system sleep; returns the time the
    # machine spent suspended.
    sleep: Callable[[int], float]
    repo: Repo
    commands: Commands
    waits: Waits = None  # type: ignore[assignment]  # set in __post_init__

    def __post_init__(self) -> None:
        if self.waits is None or (
            isinstance(self.waits, PollingWaits) and self.waits.services is not self
        ):
            object.__setattr__(self, "waits", PollingWaits(self))

    @classmethod
    def production(cls) -> Services:
        """The real console, GitHub client, clock, repo, commands, and wait
        loops."""
        ui = ConsoleUI(runtime.console)
        return cls(
            github=gh.github,
            ui=ui,
            sleep=runtime.resilient_sleep,
            repo=GitRepo(gh.github, ui),
            commands=SubprocessCommands(),
        )


class _Console(Protocol):
    def print(self, *objects: Any) -> None: ...  # noqa: ANN401

    def input(self, prompt: str) -> str: ...


class ConsoleUI:
    """A ``UI`` on a rich (or plain) console."""

    def __init__(self, console: _Console) -> None:
        self._console = console

    def print(self, *objects: object) -> None:
        self._console.print(*objects)

    def input(self, prompt: str) -> str:
        return self._console.input(prompt)

    def escape(self, text: str) -> str:
        return display._escape_markup(text)


class SubprocessCommands:
    """``Commands`` as real subprocesses."""

    def run(self, argv: list[str], *, cwd: Path, env: Mapping[str, str]) -> int:
        # Whatever autoland has printed must reach the terminal before the
        # command's own output, which bypasses this process's buffers.
        sys.stdout.flush()
        sys.stderr.flush()
        # subprocess.run (under run_shell_command) kills the command if
        # anything, such as Ctrl+C or a takeover's SIGINT, interrupts the wait
        # for it, so no command outlives the run that started it.
        return shell_commands.run_shell_command(
            argv, quiet=False, check=False, cwd=cwd, env={**os.environ, **env}
        ).returncode


class PollingWaits:
    """The waits of ``autoland.waits``, polling through *services*."""

    def __init__(self, services: Services) -> None:
        self.services = services

    def wait_for_approval(self, entry: StackEntry, *, opts: AutolandOptions) -> bool:
        return waits.wait_for_approval(entry, opts=opts, services=self.services)

    def wait_for_checks(self, entry: StackEntry, *, opts: AutolandOptions) -> bool:
        return waits.wait_for_checks(entry, opts=opts, services=self.services)

    def wait_for_mergeable(
        self, entry: StackEntry, *, opts: AutolandOptions
    ) -> MergeableResult:
        return waits.wait_for_mergeable(entry, opts=opts, services=self.services)

    def enqueue_and_wait(self, entry: StackEntry, *, opts: AutolandOptions) -> bool:
        return waits.enqueue_and_wait(entry, opts=opts, services=self.services)

    def wait_for_workflow(
        self,
        step: WorkflowStep,
        target_sha: str,
        *,
        opts: AutolandOptions,
        common: cli.CommonArgs,
        ctx: LandingContext,
    ) -> bool:
        return waits.wait_for_workflow(
            step,
            opts=opts,
            common=common,
            ctx=ctx,
            target_sha=target_sha,
            services=self.services,
        )
