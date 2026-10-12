"""Fakes for everything autoland's execution reaches through ``Services``.

Build a ``FakeServices`` with whichever fakes a test cares about, and hand its
``services`` to the code under test (a step, an execution unit, the engine, a
wait loop, a stack merge, or a command flow). Nothing is patched: each fake
records what it was asked to do, so the test reads it back afterwards.

- ``FakeUI``: records output; answers prompts from a script, then EOF.
- ``FakeWaits``: waits that succeed or fail at once, per outcome function.
- ``FakeRepo``: landed SHAs from a table; counts rebases and fetches; knows
  the ancestry it is given, plus what it learns from a fetch.
- ``FakeGitHub``: an in-memory GitHub (PR states, reviews, checks, the merge
  queue, workflow runs, GitHub stacks), whose calls can be scripted.
- ``FakeSleep``: records each sleep; can end a wait by raising
  ``StopPolling`` after a number of sleeps.

``FakeServices()`` runs against ``FakeWaits``; ``FakeServices.polling()``
runs the real wait loops of ``autoland.waits`` over the other fakes.
"""

from __future__ import annotations

import functools
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import cached_property
from typing import Any, TypeVar, cast

from stack_pr import cli
from stack_pr.autoland import gh
from stack_pr.autoland.gh import MergeQueuePollResult
from stack_pr.autoland.model import (
    LandingContext,
    PRState,
    StackEntry,
    WorkflowStep,
)
from stack_pr.autoland.options import AutolandOptions
from stack_pr.autoland.services import Services
from stack_pr.autoland.waits import MergeableResult

OPTS = AutolandOptions(
    merge_queue=True,
    required_checks=[],
    poll_interval=0,
    max_check_retries=0,
    max_queue_retries=0,
    merge_timeout=0,
    workflow_timeout=0,
    default_workflow=None,
    count=None,
    dry_run=False,
    branch=None,
    interactive=False,
    resume=False,
    state_file=None,
    always_cleanup=False,
    merge_as_stack=False,
)


def stack(*prs: int) -> list[StackEntry]:
    return [
        StackEntry(
            pr_url=f"https://github.com/o/r/pull/{n}",
            pr_number=n,
            branch=f"b{n}",
            title=f"Title {n}",
        )
        for n in prs
    ]


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------


class FakeUI:
    """Records output; answers prompts from a script, then raises EOFError."""

    def __init__(self, answers: list[str] | None = None) -> None:
        self.answers = list(answers or [])
        self.lines: list[str] = []

    def print(self, *objects: object) -> None:
        self.lines.append(" ".join(str(o) for o in objects))

    def input(self, prompt: str) -> str:
        self.lines.append(prompt)
        if not self.answers:
            raise EOFError
        return self.answers.pop(0)

    def escape(self, text: str) -> str:
        return text.replace("[", r"\[")

    @property
    def output(self) -> str:
        return "\n".join(self.lines)


# ---------------------------------------------------------------------------
# Waits
# ---------------------------------------------------------------------------

# What a fake wait does to the PR it waits on.
Outcome = Callable[[StackEntry], bool]


def ok(_entry: StackEntry) -> bool:
    return True


def fails(_entry: StackEntry) -> bool:
    return False


def merges(entry: StackEntry) -> bool:
    entry.state = PRState.MERGED
    return True


@dataclass
class FakeWaits:
    approval: Outcome = ok
    checks: Outcome = ok
    mergeable: Outcome = ok
    queue: Outcome = merges
    workflow_succeeds: bool = True
    # Each wait, in order: "approval", "checks", "mergeable", or "queue".
    calls: list[str] = field(default_factory=list)
    # The entry of each wait, in the same order as ``calls``.
    entries: list[StackEntry] = field(default_factory=list)
    workflow_steps: list[WorkflowStep] = field(default_factory=list)
    workflow_targets: list[str] = field(default_factory=list)

    def _wait(self, kind: str, entry: StackEntry, outcome: Outcome) -> bool:
        self.calls.append(kind)
        self.entries.append(entry)
        return outcome(entry)

    def waited(self, kind: str) -> list[int]:
        """The PRs a *kind* wait was made for, in order."""
        return [e.pr_number for k, e in zip(self.calls, self.entries) if k == kind]

    def wait_for_approval(self, entry: StackEntry, *, opts: AutolandOptions) -> bool:
        return self._wait("approval", entry, self.approval)

    def wait_for_checks(self, entry: StackEntry, *, opts: AutolandOptions) -> bool:
        return self._wait("checks", entry, self.checks)

    def wait_for_mergeable(
        self, entry: StackEntry, *, opts: AutolandOptions
    ) -> MergeableResult:
        return MergeableResult(ready=self._wait("mergeable", entry, self.mergeable))

    def enqueue_and_wait(self, entry: StackEntry, *, opts: AutolandOptions) -> bool:
        return self._wait("queue", entry, self.queue)

    def wait_for_workflow(
        self,
        step: WorkflowStep,
        target_sha: str,
        *,
        opts: AutolandOptions,
        common: cli.CommonArgs,
        ctx: LandingContext,
    ) -> bool:
        self.workflow_steps.append(step)
        self.workflow_targets.append(target_sha)
        step.state = "succeeded" if self.workflow_succeeds else "failed"
        return self.workflow_succeeds


# ---------------------------------------------------------------------------
# Repo
# ---------------------------------------------------------------------------


@dataclass
class FakeRepo:
    # PR number (None: the target branch head) -> its landed commit.
    shas: dict[int | None, str] = field(default_factory=dict)
    rebase_error: Exception | None = None
    # (ancestor, descendant) -> whether it is one, as far as the clone knows;
    # a pair it doesn't know is "can't tell" (None). A fetch teaches it
    # ``after_fetch``.
    ancestry: dict[tuple[str, str], bool] = field(default_factory=dict)
    after_fetch: dict[tuple[str, str], bool] = field(default_factory=dict)
    lookups: list[int | None] = field(default_factory=list)
    rebases: int = 0
    fetches: int = 0

    def fetch_target(self, common: cli.CommonArgs) -> None:
        self.fetches += 1
        self.ancestry.update(self.after_fetch)

    def is_ancestor(self, ancestor: str, descendant: str) -> bool | None:
        return self.ancestry.get((ancestor, descendant))

    def landed_sha(self, common: cli.CommonArgs, pr_number: int | None = None) -> str:
        self.lookups.append(pr_number)
        return self.shas.get(pr_number, "")

    def rebase_and_resubmit(self, common: cli.CommonArgs) -> None:
        self.rebases += 1
        if self.rebase_error is not None:
            raise self.rebase_error


# ---------------------------------------------------------------------------
# GitHub
# ---------------------------------------------------------------------------

_F = TypeVar("_F", bound=Callable[..., Any])


def _scriptable(method: _F) -> _F:
    """Log each call, and answer it from the method's script while it lasts."""

    @functools.wraps(method)
    def call(self: FakeGitHub, *args: Any, **kwargs: Any) -> Any:
        name = method.__name__
        self.calls.append((name, args))
        replies = self._scripts.get(name)
        if replies:
            reply = replies.pop(0)
            if isinstance(reply, BaseException):
                raise reply
            return reply(*args, **kwargs) if callable(reply) else reply
        return method(self, *args, **kwargs)

    return cast("_F", call)


class FakeGitHub:
    """GitHub in memory: what autoland's execution asks the ``gh.GitHub`` client.

    A PR is open unless it is in ``merged`` or ``closed``. By default it is
    approved, has no checks, is mergeable, is merged as soon as it is
    enqueued, and stays in the merge queue while open. Every call is logged
    in ``calls`` as ``(method, args)``.

    ``script(method, *replies)`` answers the next calls of *method*, one
    reply each, before it goes back to answering from the fake's state. A
    reply is returned, or raised if it is an exception, or called with the
    call's arguments if it is callable.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple]] = []
        self._scripts: dict[str, list[Any]] = {}
        self.merged: set[int] = set()
        self.closed: set[int] = set()
        # PR -> review decision (default "APPROVED"), and its checks.
        self.reviews: dict[int, str] = {}
        self.pr_checks: dict[int, list[dict]] = {}
        self.merge_commits: dict[int, str] = {}
        self.enqueued: list[int] = []
        self.booted: set[int] = set()
        # The latest runs of every workflow, newest first, and GitHub's answer
        # to whether (ancestor, descendant) is one (unknown if absent).
        self.runs: list[dict] = []
        self.compare: dict[tuple[str, str], bool] = {}
        # GitHub stacks: number -> PRs, bottom first.
        self.stacks: dict[int, list[int]] = {}
        # Every create-a-stack request, and every merge request's PR.
        self.stack_requests: list[list[int]] = []
        self.created: list[list[int]] = []
        self.unstacked: list[int] = []
        self.merge_requests: list[int] = []
        # (PR, new base) for each base change, and the base changes made by
        # the time of each merge request.
        self.base_changes: list[tuple[int, str]] = []
        self.bases_at_merge: list[list[tuple[int, str]]] = []
        self.create_error = ""
        self.merge_error = ""
        self.set_base_error = ""
        # The PRs a merge request lands (default: the whole stack up to the PR
        # it was made on), and the status GitHub then reports for it.
        self.lands: list[int] | None = None
        self.status = "merged"

    def script(self, method: str, *replies: object) -> None:
        self._scripts.setdefault(method, []).extend(replies)

    def called(self, method: str) -> list[tuple]:
        """The arguments of each call of *method*."""
        return [args for name, args in self.calls if name == method]

    # --- PRs ------------------------------------------------------------

    @_scriptable
    def owner_repo(self) -> tuple[str, str]:
        return ("o", "r")

    @_scriptable
    def pr_state(self, pr: int) -> str:
        if pr in self.merged:
            return "MERGED"
        return "CLOSED" if pr in self.closed else "OPEN"

    @_scriptable
    def review_decision(self, pr: int) -> str:
        return self.reviews.get(pr, "APPROVED")

    @_scriptable
    def merge_state(self, pr: int) -> dict:
        return {
            "state": self.pr_state(pr),
            "mergeStateStatus": "CLEAN",
            "mergeable": "MERGEABLE",
        }

    @_scriptable
    def checks(self, pr: int) -> list[dict]:
        return self.pr_checks.get(pr, [])

    @_scriptable
    def rerun_failed(self, run_ids: list[int]) -> None:
        pass

    @_scriptable
    def merge_commit(self, pr: int) -> str | None:
        return self.merge_commits.get(pr)

    # --- the merge queue -------------------------------------------------

    @_scriptable
    def in_merge_queue(self, pr: int) -> bool | None:
        return pr not in self.booted

    @_scriptable
    def has_merge_queue(self, branch: str) -> bool | None:
        return True

    @_scriptable
    def enqueue(self, pr: int) -> None:
        self.enqueued.append(pr)
        self.merged.add(pr)

    @_scriptable
    def poll_merge(self, pr: int) -> MergeQueuePollResult:
        state = self.pr_state(pr)
        if state == "MERGED":
            return MergeQueuePollResult(merged=True)
        if state == "CLOSED":
            return MergeQueuePollResult(error="PR was closed")
        if self.in_merge_queue(pr) is False:
            return MergeQueuePollResult(booted=True)
        return MergeQueuePollResult()

    # --- workflows -------------------------------------------------------

    @_scriptable
    def workflow_runs(self, workflow: str, branch: str) -> list[dict]:
        return list(self.runs)

    @_scriptable
    def contains(self, ancestor: str, descendant: str) -> bool | None:
        return self.compare.get((ancestor, descendant))

    # --- GitHub stacks ---------------------------------------------------

    @_scriptable
    def find_native_stack(self, pr: int) -> dict | None:
        for number, prs in self.stacks.items():
            if pr in prs:
                return {
                    "number": number,
                    "pull_requests": [
                        {"number": n, "state": "closed" if n in self.merged else "open"}
                        for n in prs
                    ],
                }
        return None

    @_scriptable
    def create_native_stack(self, prs: list[int]) -> dict:
        self.stack_requests.append(prs)
        if self.create_error:
            raise RuntimeError(self.create_error)
        self.created.append(prs)
        self.stacks[7] = prs
        return {"number": 7}

    @_scriptable
    def unstack_native_stack(self, number: int) -> None:
        self.unstacked.append(number)
        self.stacks.pop(number)

    @_scriptable
    def set_base(self, pr: int, base: str) -> None:
        if self.set_base_error:
            raise RuntimeError(self.set_base_error)
        self.base_changes.append((pr, base))

    @_scriptable
    def merge_async(self, pr: int, *, merge_queue: bool) -> str:
        self.merge_requests.append(pr)
        self.bases_at_merge.append(list(self.base_changes))
        if self.merge_error:
            raise RuntimeError(self.merge_error)
        prs = next(p for p in self.stacks.values() if pr in p)
        self.merged.update(self.lands if self.lands is not None else prs)
        return "uuid-1"

    @_scriptable
    def merge_async_status(self, pr: int, uuid: str) -> tuple[str, str]:
        return self.status, "conflict in PR #102" if self.status == "failed" else ""


class ForbiddenGitHub:
    """A GitHub that fails the test if anything asks it anything."""

    def __getattr__(self, name: str) -> object:
        raise AssertionError(f"GitHub was called directly: {name}")


# ---------------------------------------------------------------------------
# Sleep
# ---------------------------------------------------------------------------


class StopPolling(Exception):  # noqa: N818 - it is a signal, not an error
    """Raised by a ``FakeSleep`` out of its limit, to end a wait."""


@dataclass
class FakeSleep:
    """Records each sleep, without sleeping.

    With a *limit*, the sleep after the first *limit* ones raises
    ``StopPolling`` instead: a wait that is still waiting by then ends with it.
    """

    limit: int | None = None
    slept: list[int] = field(default_factory=list)

    def __call__(self, seconds: int) -> float:
        if self.limit is not None and len(self.slept) >= self.limit:
            raise StopPolling
        self.slept.append(seconds)
        return 0.0


def forbidden_sleep(_seconds: int) -> float:
    raise AssertionError("slept")


# ---------------------------------------------------------------------------
# All of them
# ---------------------------------------------------------------------------


@dataclass
class FakeServices:
    """A set of fakes, and the ``Services`` made of them (``services``).

    ``waits`` is ``None`` for the real wait loops, polling the other fakes
    (see ``polling``). ``github`` may be any GitHub client, e.g. a real
    ``gh.GitHub()`` over a faked ``gh``.
    """

    github: Any = field(default_factory=FakeGitHub)
    ui: FakeUI = field(default_factory=FakeUI)
    sleep: Callable[[int], float] = field(default_factory=FakeSleep)
    repo: FakeRepo = field(default_factory=FakeRepo)
    waits: FakeWaits | None = field(default_factory=FakeWaits)

    @classmethod
    def polling(cls, **fakes: Any) -> FakeServices:
        """Fakes under the real wait loops."""
        return cls(waits=None, **fakes)

    @cached_property
    def services(self) -> Services:
        return Services(
            github=cast("gh.GitHub", self.github),
            ui=self.ui,
            sleep=self.sleep,
            repo=self.repo,
            waits=cast("Any", self.waits),
        )
