"""Scripted end-to-end autoland runs, for the golden-file characterization tests.

A scenario describes a stack, the GitHub state it lands against, a plan, and
the answers typed at the console. ``run_scenario`` drives the real
``stack-pr autoland`` flow (``run_autoland``, and through it
``engine.execute_plan``) over it and returns everything a user could observe:
the console output, the last checkpoint written, ``--status`` in both formats,
and the plan as the ``-i`` editor would show it.

Every patch lives in ``_install_seams``, so a refactor that moves a seam only
has to re-point it there. The artifacts never depend on how things are patched:
they are rendered from the real console output and the real checkpoint file.

The patched seams:

- the console (``runtime.console``) is the plain-text console, with rich off,
  the Unicode glyphs pinned, and ``input()`` scripted;
- ``runtime.resilient_sleep`` doesn't sleep, but advances ``FakeWorld``'s clock;
- subprocesses: ``shell_commands.run_shell_command`` (under ``runtime.run``
  and the ``gh`` JSON helpers) and ``cli.get_command_output`` are answered by
  ``FakeWorld``, which plays both GitHub (``gh``) and the local clone (``git``);
- stack-pr itself: ``cli.get_stack`` returns the scenario's stack,
  ``cli.command_submit`` (the resubmit after a merge) does nothing, and
  ``git.get_current_branch_name`` is pinned;
- any real subprocess fails the test.
"""

from __future__ import annotations

import builtins
import configparser
import copy
import hashlib
import json
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from pytest_mock import MockerFixture

from stack_pr import cli, shell_commands
from stack_pr.autoland import commands, display, runtime
from stack_pr.autoland.display import _UNICODE_GLYPHS
from stack_pr.autoland.model import LandingContext, PRState, StackEntry
from stack_pr.autoland.plan import format_plan_for_editor, parse_plan
from stack_pr.autoland.runtime import _PlainConsole
from stack_pr.autoland.state import AutolandCheckpointer
from tests.helpers import PR_URL, common_args, mock_entry

BRANCH = "feat"
OWNER, REPO = "o", "r"
WORKFLOW_RUN_ID = 9000  # + the PR number: the CI run a PR's checks belong to


def sha(label: str) -> str:
    """A deterministic, realistic-looking commit SHA."""
    return hashlib.sha1(label.encode()).hexdigest()  # noqa: S324 - not security


def commit_sha(pr: int) -> str:
    """The local (and pushed) commit of PR *pr*."""
    return sha(f"commit-{pr}")


# ---------------------------------------------------------------------------
# The fake world: GitHub, and the local clone
# ---------------------------------------------------------------------------


@dataclass
class FakePR:
    number: int
    title: str
    state: str = "OPEN"  # OPEN, MERGED, CLOSED
    review: str = "APPROVED"
    # Clock ticks until a pending review turns into an approval (0: never).
    approve_after: int = 0
    # The checks' bucket on each successive read (pass/fail/pending); the last
    # one repeats.
    checks: list[str] = field(default_factory=lambda: ["pass"])
    merge_state: str = "CLEAN"
    # Clock ticks a PR spends in the merge queue before it merges.
    queue_ticks: int = 1
    queued_for: int | None = None  # ticks left in the queue; None: not queued
    merge_commit: str = ""

    @property
    def branch(self) -> str:
        return f"TestBot/stack/{self.number}"

    def view(self, fields: str) -> dict[str, Any]:
        values: dict[str, Any] = {
            "number": self.number,
            "title": self.title,
            "state": self.state,
            "reviewDecision": self.review,
            "mergeStateStatus": self.merge_state,
            "mergeable": "MERGEABLE",
            "mergeCommit": {"oid": self.merge_commit} if self.merge_commit else None,
        }
        return {f: values[f] for f in fields.split(",")}


@dataclass
class FakeWorld:
    """GitHub and the local clone, answering the commands autoland runs.

    Time only moves when autoland sleeps between polls (``tick``): that is when
    queued PRs merge, pending reviews come in, and workflow runs finish.
    """

    prs: dict[int, FakePR]
    # Workflow name -> the conclusion its runs reach.
    workflows: dict[str, str] = field(default_factory=dict)
    # How a stack merge request goes: "merge" (the whole stack is queued and
    # merges), or "fail" (only stack_merge_lands merge, then GitHub reports
    # stack_merge_message as the failure).
    stack_merge: str = "merge"
    stack_merge_lands: list[int] = field(default_factory=list)
    stack_merge_message: str = ""

    main_head: str = field(default_factory=lambda: sha("main-0"))
    stacks: dict[int, list[int]] = field(default_factory=dict)
    next_stack_number: int = 7
    merge_requests: dict[str, list[int]] = field(default_factory=dict)
    # Workflow name -> its runs, newest first.
    runs: dict[str, list[dict[str, str]]] = field(default_factory=dict)
    ticks: int = 0

    def merge(self, pr: FakePR) -> None:
        pr.state = "MERGED"
        pr.queued_for = None
        pr.merge_commit = sha(f"merge-{pr.number}")
        self.main_head = pr.merge_commit

    def tick(self) -> None:
        self.ticks += 1
        for pr in self.prs.values():
            if pr.queued_for is not None:
                pr.queued_for -= 1
                if pr.queued_for <= 0:
                    self.merge(pr)
            if pr.approve_after and self.ticks >= pr.approve_after:
                pr.review = "APPROVED"
        for name, runs in self.runs.items():
            for run in runs:
                if run["status"] != "completed":
                    run["status"] = "completed"
                    run["conclusion"] = self.workflows[name]

    # --- command dispatch ---------------------------------------------------

    def __call__(self, cmd: list[str], stdin: str | None) -> str:
        """Run *cmd*; return its stdout, or raise CalledProcessError."""
        if cmd[0] == "git":
            return self._git(cmd[1:])
        if cmd[0] == "gh":
            return self._gh(cmd[1:], stdin)
        raise AssertionError(f"unexpected command: {cmd}")

    def _git(self, args: list[str]) -> str:
        if args[0] in ("fetch", "rebase"):
            return ""
        if args == ["rev-parse", "origin/main"]:
            return self.main_head + "\n"
        if args[:3] == ["rev-parse", "--verify", "--quiet"]:
            number = int(args[3].rsplit("/", 1)[-1])
            return commit_sha(number) + "\n"
        if args[0] == "merge-base":
            return self.main_head + "\n"
        raise AssertionError(f"unexpected git command: {args}")

    def _gh(self, args: list[str], stdin: str | None) -> str:  # noqa: PLR0911
        sub = args[:2]
        if sub == ["repo", "view"]:
            return json.dumps({"owner": {"login": OWNER}, "name": REPO})
        if sub == ["pr", "view"]:
            return json.dumps(self.prs[int(args[2])].view(args[4]))
        if sub == ["pr", "checks"]:
            return json.dumps(self._checks(self.prs[int(args[2])]))
        if sub == ["pr", "merge"]:
            pr = self.prs[int(args[2])]
            pr.queued_for = pr.queue_ticks
            return ""
        if sub == ["pr", "edit"]:
            return ""
        if sub == ["run", "rerun"]:
            return ""
        if sub == ["run", "list"]:
            return json.dumps(self._workflow_runs(args[args.index("--workflow") + 1]))
        if sub == ["api", "graphql"]:
            return json.dumps(self._graphql(args))
        if sub[0] == "api" and args[1] == "-X":
            body = json.loads(stdin) if stdin else None
            path = args[3].removeprefix(f"repos/{OWNER}/{REPO}/")
            out = self._rest(args[2], path, body)
            return "" if out is None else json.dumps(out)
        raise AssertionError(f"unexpected gh command: {args}")

    def _checks(self, pr: FakePR) -> list[dict[str, str]]:
        bucket = pr.checks.pop(0) if len(pr.checks) > 1 else pr.checks[0]
        state = {"pass": "SUCCESS", "fail": "FAILURE", "pending": "IN_PROGRESS"}
        run = WORKFLOW_RUN_ID + pr.number
        return [
            {
                "name": "ci/test",
                "state": state[bucket],
                "bucket": bucket,
                "link": f"https://github.com/{OWNER}/{REPO}/actions/runs/{run}/job/1",
                "workflow": "CI",
            }
        ]

    def _workflow_runs(self, name: str) -> list[dict[str, str]]:
        # A push to main starts a run of every workflow on it.
        runs = self.runs.setdefault(name, [])
        if not runs or runs[0]["headSha"] != self.main_head:
            runs.insert(
                0,
                {"headSha": self.main_head, "status": "in_progress", "conclusion": ""},
            )
        return [dict(r) for r in runs[:10]]

    def _graphql(self, args: list[str]) -> dict:
        params = dict(a.split("=", 1) for a in args[3::2])
        if "number" in params:
            queued = self.prs[int(params["number"])].queued_for is not None
            entry = {"id": "MQE_1", "state": "QUEUED"} if queued else None
            return {"data": {"repository": {"pullRequest": {"mergeQueueEntry": entry}}}}
        return {"data": {"repository": {"mergeQueue": {"id": "MQ_1"}}}}

    def _stack_json(self, number: int) -> dict:
        return {
            "number": number,
            "pull_requests": [
                {"number": n, "state": self.prs[n].state.lower()}
                for n in self.stacks[number]
            ],
        }

    def _rest(self, method: str, path: str, body: dict | None) -> Any:
        if method == "GET" and path.startswith("stacks?pull_request="):
            pr = int(path.split("=", 1)[1])
            return [self._stack_json(n) for n, prs in self.stacks.items() if pr in prs]
        if method == "POST" and path == "stacks":
            assert body is not None
            number = self.next_stack_number
            self.next_stack_number += 1
            self.stacks[number] = list(body["pull_requests"])
            return self._stack_json(number)
        if method == "POST" and path.endswith("/unstack"):
            self.stacks.pop(int(path.split("/")[1]))
            return None
        if method == "PUT" and path.endswith("/merge-async"):
            return self._merge_async(int(path.split("/")[1]))
        if method == "GET" and "/merge-async/" in path:
            prs = self.merge_requests[path.rsplit("/", 1)[1]]
            if self.stack_merge == "fail":
                return {
                    "status": "failed",
                    "details": {"message": self.stack_merge_message},
                }
            merged = all(self.prs[n].state == "MERGED" for n in prs)
            return {"status": "merged" if merged else "enqueued"}
        raise AssertionError(f"unexpected gh api call: {method} {path}")

    def _merge_async(self, top: int) -> dict:
        native = next(prs for prs in self.stacks.values() if top in prs)
        prs = [
            n for n in native[: native.index(top) + 1] if self.prs[n].state == "OPEN"
        ]
        uuid = f"merge-request-{len(self.merge_requests) + 1}"
        self.merge_requests[uuid] = prs
        if self.stack_merge == "fail":
            for n in self.stack_merge_lands:
                self.merge(self.prs[n])
        else:
            for n in prs:
                self.prs[n].queued_for = self.prs[n].queue_ticks
        return {"uuid": uuid}


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------


CheckpointBuilder = Callable[
    ["FakeWorld", Path], tuple[AutolandCheckpointer, LandingContext]
]


@dataclass
class Scenario:
    name: str
    prs: list[FakePR]
    # The PRs still in the local stack, bottom first; the others have landed.
    stack: list[int]
    # The plan file's text; None runs the default plan.
    plan: str | None = None
    # Extra autoland flags (the plan file and the state file are added).
    flags: list[str] = field(default_factory=list)
    # [autoland] config, on top of the defaults below.
    config: dict[str, str] = field(default_factory=dict)
    # What the user types at each prompt; input after that is EOF.
    answers: list[str] = field(default_factory=list)
    workflows: dict[str, str] = field(default_factory=dict)
    stack_merge: str = "merge"
    stack_merge_lands: list[int] = field(default_factory=list)
    stack_merge_message: str = ""
    # PRs that landed before this run (and so are no longer in the stack).
    landed: list[int] = field(default_factory=list)
    # Builds the checkpoint of an earlier run, which this run resumes or
    # replans. Called with the world and the plan file's path.
    checkpoint: CheckpointBuilder | None = None
    expect_exit: int | None = None


DEFAULT_CONFIG = {
    "merge_queue": "true",
    "poll_interval": "120",
    "max_check_retries": "1",
    "max_queue_retries": "1",
    "workflow_timeout": "600",
    "merge_as_stack": "false",
}


def stack_entries(world: FakeWorld, prs: list[int]) -> list[StackEntry]:
    """The checkpointed stack entries for *prs*, as discovery would build them."""
    return [
        StackEntry(
            pr_url=PR_URL.format(n),
            pr_number=n,
            branch=world.prs[n].branch,
            title=world.prs[n].title,
            review_decision=world.prs[n].review,
            state=PRState.MERGED if world.prs[n].state == "MERGED" else PRState.PENDING,
        )
        for n in prs
    ]


@dataclass
class ScenarioResult:
    """What a user could observe of a run, normalized for golden files."""

    out: str  # the console output of the run
    checkpoint: str  # the last checkpoint the run wrote
    status_text: str  # `autoland --status`, over that checkpoint
    status_json: str  # `autoland --status -o json`, over that checkpoint
    plan: str  # the checkpointed plan, as the -i editor shows it
    plan_round_trip: str  # that text, parsed and formatted again
    exit_code: int | None


def _normalize(text: str, tmp_path: Path) -> str:
    for path in sorted({str(tmp_path.resolve()), str(tmp_path)}, key=len, reverse=True):
        text = text.replace(path, "<TMP>")
    # --status's "Last saved" line, in text and JSON.
    text = re.sub(
        r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d \((just now|\d+m ago)\)", "<SAVED>", text
    )
    return re.sub(r'"last_saved": "[^"]+"', '"last_saved": "<SAVED>"', text)


def _install_seams(
    mocker: MockerFixture,
    monkeypatch: pytest.MonkeyPatch,
    scenario: Scenario,
    world: FakeWorld,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    # Output: the plain console, Unicode glyphs, scripted input.
    mocker.patch.object(runtime, "console", _PlainConsole())
    mocker.patch.object(runtime, "HAVE_RICH", False)
    mocker.patch.object(display, "GLYPHS", _UNICODE_GLYPHS)
    answers = list(scenario.answers)

    def scripted_input(prompt: object = "") -> str:
        print(prompt, end="")
        if not answers:
            print()
            raise EOFError
        answer = answers.pop(0)
        print(answer)
        return answer

    monkeypatch.setattr(builtins, "input", scripted_input)

    # Time passes only when autoland sleeps between polls.
    def sleep(_seconds: int) -> float:
        world.tick()
        return 0.0

    mocker.patch.object(runtime, "resilient_sleep", side_effect=sleep)

    # Subprocesses: git and gh go to the world; anything else is a test bug.
    def run_shell_command(cmd: Any, **kwargs: Any) -> subprocess.CompletedProcess:
        args = [str(c) for c in cmd]
        try:
            out = world(args, kwargs.get("input"))
        except subprocess.CalledProcessError as e:
            return subprocess.CompletedProcess(args, e.returncode, "", e.stderr)
        return subprocess.CompletedProcess(args, 0, out, "")

    mocker.patch.object(
        shell_commands, "run_shell_command", side_effect=run_shell_command
    )
    mocker.patch.object(
        cli,
        "get_command_output",
        side_effect=lambda cmd, **_kw: world(cmd, None).strip(),
    )
    mocker.patch.object(
        subprocess, "run", side_effect=AssertionError("unexpected subprocess")
    )

    # stack-pr itself.
    raw_stack = [
        mock_entry(
            n,
            head=world.prs[n].branch,
            title=world.prs[n].title,
            commit_id=commit_sha(n),
        )
        for n in scenario.stack
    ]
    mocker.patch.object(cli, "get_stack", return_value=raw_stack)
    mocker.patch.object(cli, "command_submit")
    mocker.patch("stack_pr.git.get_current_branch_name", return_value=BRANCH)


def _autoland(argv: list[str], config: configparser.ConfigParser) -> int | None:
    """Run `stack-pr autoland <argv>`; return its exit code, or None."""
    args = cli.create_argparser(config).parse_args(["autoland", *argv])
    try:
        commands.run_autoland(common_args(base=""), args, config)
    except SystemExit as e:
        return int(e.code or 0)
    return None


def run_scenario(
    scenario: Scenario,
    *,
    mocker: MockerFixture,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> ScenarioResult:
    world = FakeWorld(
        # Copies: the world changes its PRs, and a scenario may run again.
        prs={pr.number: copy.deepcopy(pr) for pr in scenario.prs},
        workflows=dict(scenario.workflows),
        stack_merge=scenario.stack_merge,
        stack_merge_lands=scenario.stack_merge_lands,
        stack_merge_message=scenario.stack_merge_message,
    )
    for number in scenario.landed:
        world.merge(world.prs[number])
    _install_seams(mocker, monkeypatch, scenario, world, tmp_path)
    state_path = AutolandCheckpointer.default_path(BRANCH)
    config = configparser.ConfigParser()
    config["autoland"] = {**DEFAULT_CONFIG, **scenario.config}

    argv = list(scenario.flags)
    plan_file = tmp_path / "plan.autoland-plan"
    if scenario.plan is not None:
        plan_file.write_text(scenario.plan)
        if scenario.checkpoint is None:
            argv += ["--plan-file", str(plan_file)]
    if scenario.checkpoint is not None:
        old_checkpointer, old = scenario.checkpoint(world, plan_file)
        old_checkpointer.path = state_path
        old_checkpointer.save(old)

    # Keep each checkpoint as written: a run that succeeds deletes its own.
    saved: list[str] = []
    real_save = AutolandCheckpointer.save

    def save(self: AutolandCheckpointer, ctx: LandingContext) -> None:
        real_save(self, ctx)
        saved.append(self.path.read_text())

    mocker.patch.object(AutolandCheckpointer, "save", autospec=True, side_effect=save)

    capsys.readouterr()
    exit_code = _autoland(argv, config)
    out = capsys.readouterr().out
    assert saved, "the run wrote no checkpoint"

    # --status, as it would read the run's last checkpoint.
    state_path.write_text(saved[-1])
    assert _autoland(["--status"], config) is None
    status_text = capsys.readouterr().out
    assert _autoland(["--status", "-o", "json"], config) is None
    status_json = capsys.readouterr().out

    _checkpointer, ctx = AutolandCheckpointer.load(state_path)
    plan_text = format_plan_for_editor(ctx.stack, ctx.plan)
    reparsed = parse_plan(
        plan_text, ctx.stack, pr_is_merged=lambda n: world.prs[n].state == "MERGED"
    )

    return ScenarioResult(
        out=_normalize(out, tmp_path),
        checkpoint=_normalize(saved[-1], tmp_path),
        status_text=_normalize(status_text, tmp_path),
        status_json=_normalize(status_json, tmp_path),
        plan=plan_text,
        plan_round_trip=format_plan_for_editor(ctx.stack, reparsed),
        exit_code=exit_code,
    )
