"""The tools a plan step may use while it executes, injected through ``Services``.

A step reaches the console, GitHub, the wait loops, and the local repo only
through the ``Services`` in its ``StepContext``, never through module globals,
so a step can be run in isolation against fakes.

``Services.production()`` binds these to today's implementations. Each one
looks its target up through the module attribute *at call time* (``runtime.console``,
``gh.github``, ``waits.wait_for_approval``, ...) rather than holding on to the
object it found at construction, so that tests which patch those attributes
keep intercepting every call a step makes. Once tests inject fake services
directly, the delegation can bind the objects themselves.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, cast

from stack_pr.autoland import display, gh, runtime, waits
from stack_pr.autoland.model import LandingContext

if TYPE_CHECKING:
    from stack_pr import cli
    from stack_pr.autoland.model import StackEntry, WorkflowStep
    from stack_pr.autoland.options import AutolandOptions


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

    Each returns whether it succeeded; False also when the run was aborted.
    The waits record their progress on the entry or step they are given.
    """

    def wait_for_approval(
        self, entry: StackEntry, *, opts: AutolandOptions, ctx: LandingContext
    ) -> bool: ...

    def wait_for_checks(
        self, entry: StackEntry, *, opts: AutolandOptions, ctx: LandingContext
    ) -> bool: ...

    def enqueue_and_wait(
        self, entry: StackEntry, *, opts: AutolandOptions, ctx: LandingContext
    ) -> bool: ...

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

    def landed_sha(self, common: cli.CommonArgs, pr_number: int | None = None) -> str:
        """The commit of the code landed so far: *pr_number*'s merge commit,
        or else the head of the target branch. "" if it can't be found."""

    def rebase_and_resubmit(self, common: cli.CommonArgs) -> None:
        """Rebase the local stack onto the target and re-submit it. Raises on
        failure."""


@dataclass(frozen=True)
class Services:
    """Everything a step may use beyond its ``StepContext``'s run state."""

    github: gh.GitHub
    ui: UI
    waits: Waits
    repo: Repo
    # Sleep for some seconds, surviving system sleep; returns the time the
    # machine spent suspended.
    sleep: Callable[[int], float]

    @classmethod
    def production(cls) -> Services:
        """The real console, GitHub client, wait loops, and repo."""
        return cls(
            github=cast("gh.GitHub", _LiveGitHub()),
            ui=_ConsoleUI(),
            waits=_ModuleWaits(),
            repo=_GitRepo(),
            sleep=_resilient_sleep,
        )


# ---------------------------------------------------------------------------
# Production implementations: thin delegates resolved at call time
# ---------------------------------------------------------------------------


class _LiveGitHub:
    """Forwards every attribute to whatever ``gh.github`` is when it is used."""

    def __getattr__(self, name: str) -> object:
        return getattr(gh.github, name)


def _resilient_sleep(seconds: int) -> float:
    return runtime.resilient_sleep(seconds)


class _ConsoleUI:
    def print(self, *objects: object) -> None:
        runtime.console.print(*objects)

    def input(self, prompt: str) -> str:
        return runtime.console.input(prompt)

    def escape(self, text: str) -> str:
        return display._escape_markup(text)


class _ModuleWaits:
    def wait_for_approval(
        self, entry: StackEntry, *, opts: AutolandOptions, ctx: LandingContext
    ) -> bool:
        return waits.wait_for_approval(entry, opts=opts, ctx=ctx)

    def wait_for_checks(
        self, entry: StackEntry, *, opts: AutolandOptions, ctx: LandingContext
    ) -> bool:
        return waits.wait_for_checks(entry, opts=opts, ctx=ctx)

    def enqueue_and_wait(
        self, entry: StackEntry, *, opts: AutolandOptions, ctx: LandingContext
    ) -> bool:
        return waits.enqueue_and_wait(entry, opts=opts, ctx=ctx)

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
            step, opts=opts, common=common, ctx=ctx, target_sha=target_sha
        )


class _GitRepo:
    def landed_sha(self, common: cli.CommonArgs, pr_number: int | None = None) -> str:
        # Goes through waits._refresh_last_landed_sha, which records into a
        # context, so that tests patching it keep intercepting the lookup. A
        # scratch context keeps the run's own state untouched: the engine
        # records what the step reports.
        scratch = LandingContext()
        if pr_number is None:
            waits._refresh_last_landed_sha(scratch, common)
        else:
            waits._refresh_last_landed_sha(scratch, common, pr_number)
        return scratch.last_landed_sha

    def rebase_and_resubmit(self, common: cli.CommonArgs) -> None:
        waits.rebase_and_resubmit(common)
