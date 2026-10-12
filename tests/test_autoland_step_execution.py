"""Executing each step type in isolation, against fake services.

Each test builds a ``StepContext`` by hand and injects fakes for the console,
the waits, and the repo, with no module patching: this is the whole contract
between a step and the engine that runs it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

import pytest

from stack_pr import cli
from stack_pr.autoland import engine, gh
from stack_pr.autoland.model import (
    ConfirmStep,
    LandingContext,
    LandStep,
    PRState,
    StackEntry,
    WorkflowStep,
)
from stack_pr.autoland.options import AutolandOptions
from stack_pr.autoland.services import Services
from stack_pr.autoland.state import AutolandCheckpointer
from stack_pr.autoland.steps import AutolandStep, StepContext, StepResult
from tests.helpers import common_args

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


# ---------------------------------------------------------------------------
# Fakes
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
    queue: Outcome = merges
    workflow_succeeds: bool = True
    calls: list[str] = field(default_factory=list)
    workflow_targets: list[str] = field(default_factory=list)

    def wait_for_approval(self, entry: StackEntry, *, opts: AutolandOptions) -> bool:
        self.calls.append("approval")
        return self.approval(entry)

    def wait_for_checks(self, entry: StackEntry, *, opts: AutolandOptions) -> bool:
        self.calls.append("checks")
        return self.checks(entry)

    def enqueue_and_wait(self, entry: StackEntry, *, opts: AutolandOptions) -> bool:
        self.calls.append("queue")
        return self.queue(entry)

    def wait_for_workflow(
        self,
        step: WorkflowStep,
        target_sha: str,
        *,
        opts: AutolandOptions,
        common: cli.CommonArgs,
        ctx: LandingContext,
    ) -> bool:
        self.workflow_targets.append(target_sha)
        step.state = "succeeded" if self.workflow_succeeds else "failed"
        return self.workflow_succeeds


@dataclass
class FakeRepo:
    # PR number (None: the target branch head) -> its landed commit.
    shas: dict[int | None, str] = field(default_factory=dict)
    rebase_error: Exception | None = None
    lookups: list[int | None] = field(default_factory=list)
    rebases: int = 0

    def landed_sha(self, common: cli.CommonArgs, pr_number: int | None = None) -> str:
        self.lookups.append(pr_number)
        return self.shas.get(pr_number, "")

    def rebase_and_resubmit(self, common: cli.CommonArgs) -> None:
        self.rebases += 1
        if self.rebase_error is not None:
            raise self.rebase_error


class _NoGitHub:
    def __getattr__(self, name: str) -> object:
        raise AssertionError(f"a step called GitHub directly: {name}")


def services(
    ui: FakeUI | None = None,
    waits: FakeWaits | None = None,
    repo: FakeRepo | None = None,
) -> Services:
    def no_sleep(_seconds: int) -> float:
        raise AssertionError("a step slept")

    return Services(
        github=cast("gh.GitHub", _NoGitHub()),
        ui=ui or FakeUI(),
        waits=waits or FakeWaits(),
        repo=repo or FakeRepo(),
        sleep=no_sleep,
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


def run_step(
    step: AutolandStep,
    svc: Services,
    *,
    entries: list[StackEntry] | None = None,
    upcoming: list[str] | None = None,
    landed_sha: str = "",
    ends_landed_prefix: bool = False,
) -> StepResult:
    """Execute *step* as step 2 of a 3-step plan."""
    run = LandingContext(stack=entries or stack(101, 102), plan=[step])
    return step.execute(
        StepContext(
            index=1,
            total=3,
            run=run,
            opts=OPTS,
            common=common_args(),
            services=svc,
            checkpoint=lambda: None,
            upcoming=lambda: list(upcoming or []),
            landed_sha=lambda: landed_sha,
            ends_landed_prefix=ends_landed_prefix,
        )
    )


# ---------------------------------------------------------------------------
# LandStep
# ---------------------------------------------------------------------------


def test_land_step_lands_and_rebases_the_stack_above() -> None:
    waits, repo, ui = FakeWaits(), FakeRepo(shas={101: "sha101"}), FakeUI()
    entries = stack(101, 102)

    result = run_step(
        LandStep(entry_index=0), services(ui, waits, repo), entries=entries
    )

    assert result == StepResult(landed_sha="sha101")
    assert waits.calls == ["approval", "checks", "queue"]
    assert entries[0].is_merged()
    assert repo.lookups == [101]
    assert repo.rebases == 1
    assert "Step 2/3: Landing PR #101 — Title 101" in ui.output


def test_land_step_at_the_top_of_the_stack_does_not_rebase() -> None:
    repo = FakeRepo(shas={102: "sha102"})

    result = run_step(LandStep(entry_index=1), services(repo=repo))

    assert result == StepResult(landed_sha="sha102")
    assert repo.rebases == 0


def test_land_step_stops_waiting_once_the_pr_has_merged() -> None:
    # Merged by hand while waiting for approval: no checks, no queue.
    waits = FakeWaits(approval=merges)

    result = run_step(LandStep(entry_index=0), services(waits=waits))

    assert not result.aborted
    assert waits.calls == ["approval"]


def test_land_step_skips_a_pr_that_is_already_merged() -> None:
    waits, repo, ui = FakeWaits(), FakeRepo(shas={101: "sha101"}), FakeUI()
    entries = stack(101, 102)
    entries[0].state = PRState.MERGED

    result = run_step(
        LandStep(entry_index=0), services(ui, waits, repo), entries=entries
    )

    assert result == StepResult(landed_sha="sha101")
    assert waits.calls == []
    assert repo.rebases == 0
    assert "PR #101 already merged, skipping" in ui.output


@pytest.mark.parametrize(
    ("ends_landed_prefix", "landed_sha", "lookups"),
    [(True, "sha99", [99]), (False, "", [])],
)
def test_already_landed_step_reports_its_sha_only_at_the_end_of_the_prefix(
    ends_landed_prefix: bool, landed_sha: str, lookups: list[int]
) -> None:
    repo, ui = FakeRepo(shas={99: "sha99"}), FakeUI()

    result = run_step(
        LandStep(entry_index=-1, pr_number=99),
        services(ui, repo=repo),
        ends_landed_prefix=ends_landed_prefix,
    )

    assert result == StepResult(landed_sha=landed_sha)
    assert repo.lookups == lookups
    assert "PR #99 already landed, skipping" in ui.output


@pytest.mark.parametrize(
    ("waits", "reason"),
    [
        (FakeWaits(approval=fails), "PR #101 was closed while waiting for approval"),
        (FakeWaits(checks=fails), "PR #101 checks failed after retries"),
        (FakeWaits(queue=fails), "PR #101 failed to merge"),
    ],
    ids=["approval", "checks", "queue"],
)
def test_land_step_aborts_when_a_wait_fails(waits: FakeWaits, reason: str) -> None:
    repo = FakeRepo(shas={101: "sha101"})

    result = run_step(LandStep(entry_index=0), services(waits=waits, repo=repo))

    assert result == StepResult(abort_reason=reason)
    assert repo.rebases == 0


def test_land_step_aborts_on_a_failed_rebase_but_reports_what_landed() -> None:
    repo = FakeRepo(shas={101: "sha101"}, rebase_error=RuntimeError("conflict"))

    result = run_step(LandStep(entry_index=0), services(repo=repo))

    assert result == StepResult(
        abort_reason="Rebase failed after merging #101: conflict",
        landed_sha="sha101",
    )


# ---------------------------------------------------------------------------
# WorkflowStep
# ---------------------------------------------------------------------------


def test_workflow_step_waits_for_the_landed_code() -> None:
    waits, ui = FakeWaits(), FakeUI()
    step = WorkflowStep(workflow="deploy.yaml")

    result = run_step(step, services(ui, waits), landed_sha="abc123")

    assert result == StepResult()
    assert waits.workflow_targets == ["abc123"]
    assert step.state == "succeeded"
    assert "Step 2/3: Workflow checkpoint — deploy.yaml" in ui.output


def test_workflow_step_aborts_when_the_workflow_fails() -> None:
    step = WorkflowStep(workflow="deploy.yaml")

    result = run_step(step, services(waits=FakeWaits(workflow_succeeds=False)))

    assert result == StepResult(abort_reason="Workflow deploy.yaml failed or timed out")
    assert step.state == "failed"


@pytest.mark.parametrize("state", ["skipped", "succeeded"])
def test_workflow_step_already_done_does_nothing(state: str) -> None:
    waits, ui = FakeWaits(), FakeUI()

    result = run_step(WorkflowStep(workflow="d.yaml", state=state), services(ui, waits))

    assert result == StepResult()
    assert waits.workflow_targets == []
    assert ui.lines == []


# ---------------------------------------------------------------------------
# ConfirmStep
# ---------------------------------------------------------------------------


def test_confirm_step_waits_for_y() -> None:
    ui = FakeUI(answers=["n", "Y"])
    step = ConfirmStep(condition="QA [done]")

    result = run_step(step, services(ui), upcoming=["Land PR #7: X [1,25] Y", "B"])

    assert result == StepResult()
    assert step.confirmed
    output = ui.output
    assert "Step 2/3: Manual confirmation required" in output
    assert r'Confirm "QA \[done]" is complete' in output  # escaped
    # The steps after this one, numbered from 1, with markup escaped.
    assert "Next steps:" in output
    assert r"  1. Land PR #7: X \[1,25] Y" in output
    assert "  2. B" in output
    assert "Type 'y' or 'Y' to confirm." in output  # after the "n"
    assert ui.lines[-1] == "[green]Confirmed[/green]"


def test_confirm_step_as_the_final_step() -> None:
    ui = FakeUI(answers=["y"])

    run_step(ConfirmStep(), services(ui))

    assert "Ready to proceed?" in ui.output
    assert "This is the final step in the plan." in ui.output
    assert "Next steps:" not in ui.output


def test_confirm_step_aborts_on_eof() -> None:
    step = ConfirmStep()

    result = run_step(step, services(FakeUI()))

    assert result == StepResult(
        abort_reason=(
            "Confirm step received EOF — cannot confirm in non-interactive mode"
        )
    )
    assert not step.confirmed


def test_confirmed_step_does_not_prompt() -> None:
    ui = FakeUI()

    assert run_step(ConfirmStep(confirmed=True), services(ui)) == StepResult()
    assert ui.lines == []


# ---------------------------------------------------------------------------
# The engine records what steps report
# ---------------------------------------------------------------------------


def test_engine_passes_a_landed_sha_on_to_later_steps(tmp_path: Path) -> None:
    waits = FakeWaits()
    repo = FakeRepo(shas={101: "sha101", None: "head"})
    plan: list[AutolandStep] = [
        WorkflowStep(workflow="before.yaml"),
        LandStep(entry_index=0, pr_number=101),
        WorkflowStep(workflow="after.yaml"),
    ]
    ctx = LandingContext(stack=stack(101, 102), plan=plan)
    checkpointer = AutolandCheckpointer(
        path=tmp_path / "state.json", branch="feat", base="main"
    )

    assert engine.execute_plan(
        ctx,
        common_args(),
        OPTS,
        checkpointer,
        services=services(FakeUI(), waits, repo),
    )

    # Before anything lands, a workflow follows the target branch's head; after
    # the land step, the commit it reported.
    assert waits.workflow_targets == ["head", "sha101"]
    assert ctx.last_landed_sha == "sha101"
    assert ctx.current_step == len(plan)


def test_engine_records_the_reason_a_step_aborted(tmp_path: Path) -> None:
    ctx = LandingContext(stack=stack(101), plan=[ConfirmStep()])
    checkpointer = AutolandCheckpointer(
        path=tmp_path / "state.json", branch="feat", base="main"
    )

    assert not engine.execute_plan(
        ctx, common_args(), OPTS, checkpointer, services=services(FakeUI())
    )

    assert ctx.abort_reason.startswith("Confirm step received EOF")
    _, saved = AutolandCheckpointer.load(checkpointer.path)
    assert saved.abort_reason == ctx.abort_reason
