"""Executing each step type in isolation, against fake services.

Each test builds a ``StepContext`` by hand and injects fakes for the console,
the waits, and the repo, with no module patching: this is the whole contract
between a step and the engine that runs it.
"""

from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path

import pytest

from stack_pr.autoland import engine
from stack_pr.autoland.model import (
    ConfirmStep,
    ExecStep,
    LandingContext,
    LandStep,
    PRState,
    StackEntry,
    WorkflowStep,
)
from stack_pr.autoland.services import Services, SubprocessCommands
from stack_pr.autoland.state import AutolandCheckpointer
from stack_pr.autoland.steps import AutolandStep, StepContext, StepResult
from tests.autoland_fakes import (
    OPTS,
    FakeCommands,
    FakeRepo,
    FakeServices,
    FakeUI,
    FakeWaits,
    ForbiddenGitHub,
    fails,
    forbidden_sleep,
    merges,
    stack,
)
from tests.helpers import common_args


def services(
    ui: FakeUI | None = None,
    waits: FakeWaits | None = None,
    repo: FakeRepo | None = None,
    commands: FakeCommands | None = None,
) -> Services:
    """Fake services for a step, which must not call GitHub or sleep itself:
    it waits only through ``waits``."""
    return FakeServices(
        github=ForbiddenGitHub(),
        ui=ui or FakeUI(),
        sleep=forbidden_sleep,
        repo=repo or FakeRepo(),
        commands=commands or FakeCommands(),
        waits=waits or FakeWaits(),
    ).services


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
    assert waits.calls == ["approval", "checks", "merge"]
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
        (FakeWaits(merge=fails), "PR #101 failed to merge"),
    ],
    ids=["approval", "checks", "merge"],
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
# ExecStep
# ---------------------------------------------------------------------------


def run_exec(
    step: ExecStep, svc: Services, *, landed_sha: str = "sha102"
) -> StepResult:
    """Execute *step* after a plan that landed #100 in an earlier run, then
    #101 and #102 (with a confirmation between them)."""
    entries = stack(101, 102, 103)
    entries[0].state = entries[1].state = PRState.MERGED
    plan: list[AutolandStep] = [
        LandStep(entry_index=-1, pr_number=100),
        LandStep(entry_index=0),
        ConfirmStep(confirmed=True),
        LandStep(entry_index=1, pr_number=102),
        step,
        LandStep(entry_index=2),
    ]
    return step.execute(
        StepContext(
            index=4,
            total=len(plan),
            run=LandingContext(stack=entries, plan=plan),
            opts=OPTS,
            common=common_args(remote="upstream", target="trunk"),
            services=svc,
            checkpoint=lambda: None,
            upcoming=list,
            landed_sha=lambda: landed_sha,
            branch="feat",
        )
    )


def test_exec_step_runs_its_command_in_the_repo_with_the_run_in_its_env() -> None:
    commands, ui = FakeCommands(), FakeUI()
    repo = FakeRepo(top_level=Path("/work/worktree/repo"))
    step = ExecStep(command="./provision.sh --env 'staging east' $HOME")

    result = run_exec(step, services(ui, repo=repo, commands=commands))

    assert result == StepResult()
    assert step.state == "succeeded"
    [run] = commands.runs
    # Split like a shell would, but nothing is expanded: there is no shell.
    assert run.argv == ["./provision.sh", "--env", "staging east", "$HOME"]
    assert run.cwd == Path("/work/worktree/repo")
    assert run.env == {
        "AUTOLAND_LANDED_SHA": "sha102",
        "AUTOLAND_LANDED_PRS": "100 101 102",
        "AUTOLAND_BRANCH": "feat",
        "AUTOLAND_REMOTE": "upstream",
        "AUTOLAND_TARGET": "trunk",
    }
    assert "Step 5/6: Run ./provision.sh" in ui.output


def test_exec_step_shows_it_is_running_in_the_checkpoint() -> None:
    states: list[str] = []
    step = ExecStep(command="true")
    entries = stack(101)

    step.execute(
        StepContext(
            index=0,
            total=2,
            run=LandingContext(stack=entries, plan=[step, LandStep(entry_index=0)]),
            opts=OPTS,
            common=common_args(),
            services=services(),
            checkpoint=lambda: states.append(step.state),
            upcoming=list,
            landed_sha=lambda: "",
        )
    )

    assert states == ["running"]
    assert step.state == "succeeded"


@pytest.mark.parametrize(
    ("result", "reason"),
    [
        (3, "Command `./smoke-test.sh` exited with status 3"),
        (-9, "Command `./smoke-test.sh` killed by signal 9"),
        (
            FileNotFoundError(2, "No such file or directory"),
            "Command `./smoke-test.sh` could not start: No such file or directory",
        ),
        (
            PermissionError(13, "Permission denied"),
            "Command `./smoke-test.sh` could not start: Permission denied",
        ),
    ],
)
def test_exec_step_aborts_unless_its_command_succeeds(
    result: int | OSError, reason: str
) -> None:
    step = ExecStep(command="./smoke-test.sh")

    outcome = run_exec(step, services(commands=FakeCommands(results=[result])))

    assert outcome == StepResult(abort_reason=reason)
    assert step.state == "failed"
    assert step.error_message == reason.replace("Command `./smoke-test.sh`", "Command")


@pytest.mark.parametrize("state", ["skipped", "succeeded"])
def test_exec_step_already_done_does_not_run(state: str) -> None:
    commands, ui = FakeCommands(), FakeUI()

    result = run_exec(
        ExecStep(command="./provision.sh", state=state),
        services(ui, commands=commands),
    )

    assert result == StepResult()
    assert commands.runs == []
    assert ui.lines == []


@pytest.mark.parametrize("state", ["failed", "running"])
def test_exec_step_runs_again_after_failing_or_being_interrupted(state: str) -> None:
    commands = FakeCommands()
    step = ExecStep(command="./provision.sh", state=state, error_message="boom")

    result = run_exec(step, services(commands=commands))

    assert result == StepResult()
    assert len(commands.runs) == 1
    assert (step.state, step.error_message) == ("succeeded", "")


_ENV_SCRIPT = """\
import json, os, sys
names = [n for n in os.environ if n.startswith("AUTOLAND_")] + ["INHERITED"]
facts = {"cwd": os.getcwd(), "args": sys.argv[1:]}
facts.update({n: os.environ.get(n) for n in names})
with open(os.environ["OUT"], "w") as f:
    json.dump(facts, f)
sys.exit(int(sys.argv[1]))
"""


@pytest.mark.parametrize("status", [0, 4])
def test_exec_step_runs_a_real_command(
    status: int, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = tmp_path / "script.py"
    script.write_text(_ENV_SCRIPT)
    out = tmp_path / "facts.json"
    monkeypatch.setenv("OUT", str(out))
    monkeypatch.setenv("INHERITED", "yes")
    root = tmp_path / "repo"
    root.mkdir()
    svc = FakeServices(
        ui=FakeUI(),
        repo=FakeRepo(top_level=root),
        commands=SubprocessCommands(),  # type: ignore[arg-type]
    ).services
    step = ExecStep(
        command=shlex.join([sys.executable, str(script), str(status), "a b"])
    )

    result = run_exec(step, svc)

    assert result.aborted == (status != 0)
    assert step.state == ("succeeded" if status == 0 else "failed")
    assert json.loads(out.read_text()) == {
        "cwd": str(root.resolve()),
        "args": [str(status), "a b"],
        "AUTOLAND_LANDED_SHA": "sha102",
        "AUTOLAND_LANDED_PRS": "100 101 102",
        "AUTOLAND_BRANCH": "feat",
        "AUTOLAND_REMOTE": "upstream",
        "AUTOLAND_TARGET": "trunk",
        "INHERITED": "yes",
    }


def test_exec_step_reports_a_missing_program(tmp_path: Path) -> None:
    svc = FakeServices(
        repo=FakeRepo(top_level=tmp_path),
        commands=SubprocessCommands(),  # type: ignore[arg-type]
    ).services
    step = ExecStep(command="./no-such-script.sh")

    result = run_exec(step, svc)

    assert result.abort_reason == (
        "Command `./no-such-script.sh` could not start: No such file or directory"
    )


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
