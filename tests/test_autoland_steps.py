"""The plan step registry, and each step type's plan syntax, persistence, and
view."""

from __future__ import annotations

import dataclasses
import inspect
import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from stack_pr.autoland.model import LandingContext, PRState, StackEntry
from stack_pr.autoland.plan import (
    carry_over_progress,
    format_plan_for_editor,
    parse_plan,
)
from stack_pr.autoland.state import AutolandCheckpointer
from stack_pr.autoland.steps import (
    REGISTRY,
    AutolandStep,
    ConfirmStep,
    ExecStep,
    LandStep,
    PlanParser,
    StepContext,
    StepOutcome,
    StepRegistry,
    StepResult,
    StepTone,
    StepView,
    WorkflowStep,
)

STACK = [
    StackEntry(pr_url=f"https://github.com/o/r/pull/{n}", pr_number=n, branch=f"b{n}")
    for n in (101, 102)
]

# One freshly parsed instance of every step type, as a plan line would produce.
# Adding a step type without adding it here fails the coverage test below.
SAMPLES: dict[type[AutolandStep], AutolandStep] = {
    LandStep: LandStep(entry_index=0, pr_number=101),
    WorkflowStep: WorkflowStep(workflow="deploy.yaml"),
    ConfirmStep: ConfirmStep(condition="QA sign-off complete"),
    ExecStep: ExecStep(command="./provision.sh --env 'staging east'"),
}


def test_every_registered_step_type_has_a_sample() -> None:
    assert set(REGISTRY.types()) == set(SAMPLES)


@pytest.mark.parametrize("step_type", REGISTRY.types(), ids=lambda t: t.__name__)
def test_every_registered_step_type_has_a_view(
    step_type: type[AutolandStep],
) -> None:
    # A step type with no view of its own would leave view() abstract.
    assert not inspect.isabstract(step_type)
    view = SAMPLES[step_type].view(STACK, is_next=False, assumed_completed=False)
    assert isinstance(view, StepView)
    assert view.status
    assert view.key
    assert view.description


@pytest.mark.parametrize(
    ("kind", "keyword", "step_type"),
    [
        ("land", "l", LandStep),
        ("workflow", "w", WorkflowStep),
        ("confirm", "c", ConfirmStep),
        ("exec", "x", ExecStep),
    ],
)
def test_registry_looks_up_step_types(
    kind: str, keyword: str, step_type: type[AutolandStep]
) -> None:
    assert REGISTRY.for_kind(kind) is step_type
    assert REGISTRY.for_keyword(keyword) is step_type


def test_registry_unknown_keyword_is_none() -> None:
    assert REGISTRY.for_keyword("z") is None


def test_registry_unknown_kind_is_an_error() -> None:
    with pytest.raises(ValueError, match="Unknown plan step type: 'bogus'"):
        REGISTRY.for_kind("bogus")


@dataclass
class _Dummy(AutolandStep):
    kind = "dummy"
    keyword = "d"
    label = "Dummy"

    @classmethod
    def parse(cls, args: str, parser: PlanParser) -> _Dummy:
        return cls()

    def plan_line(self, stack: list[StackEntry]) -> tuple[str, str]:
        return "d", ""

    def view(
        self, stack: list[StackEntry], *, is_next: bool, assumed_completed: bool
    ) -> StepView:
        return StepView(
            outcome=StepOutcome.PENDING,
            status="Pending",
            tone=StepTone.MUTED,
            key="d",
            description="Dummy",
        )

    def execute(self, sc: StepContext) -> StepResult:
        return StepResult.done()


@pytest.mark.parametrize(
    ("kind", "keyword", "message"),
    [
        ("land", "d", "Step kind 'land' is already registered by LandStep"),
        ("dummy", "l", "Step keyword 'l' is already registered by LandStep"),
    ],
)
def test_registry_rejects_duplicate_registration(
    kind: str, keyword: str, message: str
) -> None:
    registry = StepRegistry()
    registry.register(LandStep)
    clash = type("Clash", (_Dummy,), {"kind": kind, "keyword": keyword})
    with pytest.raises(ValueError, match=message):
        registry.register(clash)
    # A rejected type is not half-registered under its other name.
    assert registry.types() == [LandStep]
    assert registry.for_keyword("d") is None


@pytest.mark.parametrize("step", SAMPLES.values(), ids=lambda s: type(s).__name__)
def test_step_survives_state_file_round_trip(
    step: AutolandStep, tmp_path: Path
) -> None:
    sf = tmp_path / "state.json"
    ctx = LandingContext(stack=STACK, plan=[step])
    AutolandCheckpointer(path=sf, branch="feat", base="main").save(ctx)

    assert json.loads(sf.read_text())["plan"][0]["type"] == step.kind
    _, loaded = AutolandCheckpointer.load(sf)
    assert loaded.plan == [step]


@pytest.mark.parametrize("step", SAMPLES.values(), ids=lambda s: type(s).__name__)
def test_step_survives_plan_text_round_trip(step: AutolandStep) -> None:
    # Every plan must land something, so pair a non-land step with a land.
    plan = [step] if isinstance(step, LandStep) else [SAMPLES[LandStep], step]
    text = format_plan_for_editor(STACK, plan)
    assert parse_plan(text, STACK) == plan


def test_unknown_step_type_in_state_file_is_an_error(tmp_path: Path) -> None:
    sf = tmp_path / "state.json"
    ctx = LandingContext(stack=STACK, plan=[SAMPLES[LandStep]])
    AutolandCheckpointer(path=sf, branch="feat", base="main").save(ctx)
    data = json.loads(sf.read_text())
    data["plan"].append({"type": "bogus", "condition": ""})
    sf.write_text(json.dumps(data))

    with pytest.raises(ValueError, match="Unknown plan step type: 'bogus'"):
        AutolandCheckpointer.load(sf)


# --- views -------------------------------------------------------------------
#
# A step's view is everything the status panel, the "Next steps" list, and
# `--status -o json` say about it. These pin the contract per step type.


def _entry(**changes: object) -> StackEntry:
    return dataclasses.replace(STACK[0], **{"title": "Fix the thing", **changes})


def _view(
    step: AutolandStep,
    stack: list[StackEntry] = STACK,
    *,
    is_next: bool = False,
    assumed_completed: bool = False,
) -> StepView:
    return step.view(stack, is_next=is_next, assumed_completed=assumed_completed)


def test_land_view_of_a_pending_pr() -> None:
    assert _view(LandStep(entry_index=0, pr_number=101), [_entry()]) == StepView(
        outcome=StepOutcome.PENDING,
        status="Pending",
        tone=StepTone.MUTED,
        key="#101",
        note="Fix the thing",
        description="Land PR #101: Fix the thing",
        data={
            "pr_number": 101,
            "pr_url": "https://github.com/o/r/pull/101",
            "title": "Fix the thing",
        },
    )


@pytest.mark.parametrize(
    ("state", "outcome", "status", "tone"),
    [
        (
            PRState.WAITING_FOR_APPROVAL,
            StepOutcome.ACTIVE,
            "Waiting for approval",
            StepTone.AWAITING_REVIEW,
        ),
        (
            PRState.WAITING_FOR_CHECKS,
            StepOutcome.ACTIVE,
            "Waiting for checks",
            StepTone.AWAITING_CHECKS,
        ),
        (
            PRState.IN_MERGE_QUEUE,
            StepOutcome.ACTIVE,
            "In merge queue",
            StepTone.IN_QUEUE,
        ),
        (PRState.MERGING, StepOutcome.ACTIVE, "Merging", StepTone.MERGING),
        (
            PRState.WAITING_FOR_WORKFLOW,
            StepOutcome.ACTIVE,
            "Waiting for workflow",
            StepTone.AWAITING_WORKFLOW,
        ),
        (PRState.MERGED, StepOutcome.DONE, "Merged", StepTone.SUCCESS),
        (PRState.FAILED, StepOutcome.FAILED, "Failed", StepTone.FAILURE),
    ],
)
def test_land_view_follows_the_pr_state(
    state: PRState, outcome: StepOutcome, status: str, tone: StepTone
) -> None:
    view = _view(LandStep(entry_index=0), [_entry(state=state)])
    assert (view.outcome, view.status, view.tone) == (outcome, status, tone)


def test_land_view_detail_has_the_error_and_retries_once_retried() -> None:
    step = LandStep(entry_index=0)
    assert _view(step, [_entry(error_message="Checks failed")]).detail == (
        "Checks failed",
    )
    entry = _entry(error_message="Checks failed", check_retries=2, queue_retries=1)
    assert _view(step, [entry]).detail == ("Checks failed", "retries CI 2 / MQ 1")


def test_land_view_of_an_untitled_pr() -> None:
    view = _view(LandStep(entry_index=0), [_entry(title="")])
    assert view.note == "(untitled)"
    assert view.description == "Land PR #101: (untitled)"
    # JSON reports the title as GitHub has it, not the placeholder.
    assert view.data["title"] == ""


def test_land_view_of_an_already_landed_pr() -> None:
    assert _view(LandStep(entry_index=-1, pr_number=99)) == StepView(
        outcome=StepOutcome.DONE,
        status="Landed",
        tone=StepTone.SUCCESS,
        key="#99",
        description="Land PR #99 (already landed)",
        data={"pr_number": 99, "pr_url": None, "title": None},
    )


@pytest.mark.parametrize(
    ("state", "outcome", "status", "tone"),
    [
        ("pending", StepOutcome.PENDING, "Pending", StepTone.MUTED),
        (
            "waiting",
            StepOutcome.ACTIVE,
            "Waiting for workflow",
            StepTone.AWAITING_WORKFLOW,
        ),
        ("succeeded", StepOutcome.DONE, "Workflow complete", StepTone.SUCCESS),
        ("failed", StepOutcome.FAILED, "Failed", StepTone.FAILURE),
        # Only a workflow inside the landed prefix is "skipped".
        ("skipped", StepOutcome.DONE, "Assumed completed", StepTone.SUCCESS),
    ],
)
def test_workflow_view_follows_its_state(
    state: str, outcome: StepOutcome, status: str, tone: StepTone
) -> None:
    assert _view(WorkflowStep(workflow="deploy.yaml", state=state)) == StepView(
        outcome=outcome,
        status=status,
        tone=tone,
        key="deploy.yaml",
        description="Wait for workflow deploy.yaml",
        data={"workflow": "deploy.yaml"},
    )


def test_workflow_view_detail_is_its_error() -> None:
    step = WorkflowStep(workflow="deploy.yaml", state="failed", error_message="boom")
    assert _view(step).detail == ("boom",)


@pytest.mark.parametrize(
    ("step", "is_next", "assumed_completed", "expected"),
    [
        (
            ConfirmStep(condition="QA"),
            False,
            False,
            (StepOutcome.PENDING, "Pending", StepTone.MUTED),
        ),
        (
            ConfirmStep(condition="QA"),
            True,
            False,
            (StepOutcome.ACTIVE, "Awaiting confirmation", StepTone.AWAITING_USER),
        ),
        (
            ConfirmStep(condition="QA", confirmed=True),
            True,
            False,
            (StepOutcome.DONE, "Confirmed", StepTone.SUCCESS),
        ),
        # Inside the landed prefix nobody confirmed it in this run.
        (
            ConfirmStep(condition="QA", confirmed=True),
            False,
            True,
            (StepOutcome.DONE, "Assumed completed", StepTone.SUCCESS),
        ),
    ],
)
def test_confirm_view_states(
    step: ConfirmStep,
    is_next: bool,
    assumed_completed: bool,
    expected: tuple[StepOutcome, str, StepTone],
) -> None:
    outcome, status, tone = expected
    assert _view(step, is_next=is_next, assumed_completed=assumed_completed) == (
        StepView(
            outcome=outcome,
            status=status,
            tone=tone,
            key="QA",
            description="Manual confirmation: QA",
            data={"condition": "QA"},
        )
    )


def test_confirm_view_without_a_condition() -> None:
    view = _view(ConfirmStep())
    assert view.key == "(no condition)"
    assert view.description == "Manual confirmation"
    assert view.data == {"condition": ""}


# ---------------------------------------------------------------------------
# ExecStep
# ---------------------------------------------------------------------------


def test_exec_step_keeps_its_command_as_written() -> None:
    # Spacing and quoting are the command's own; a ' #' still starts a comment.
    [_, step] = parse_plan(
        "l\nx  bash -c 'echo  \"$X\" | wc'   # count it\n",
        STACK,
        pr_is_merged=lambda _n: False,
    )
    assert step == ExecStep(command="bash -c 'echo  \"$X\" | wc'")
    assert step.plan_line(STACK) == ("x bash -c 'echo  \"$X\" | wc'", "")


@pytest.mark.parametrize(
    ("line", "message"),
    [
        ("x", "Line 2: unrecognized step: 'x'"),
        ("x   # nothing to run", "Line 2: unrecognized step"),
        ("x echo 'unclosed", "Line 2: malformed command .*No closing quotation"),
        # ' #' cuts the line even inside quotes, as for every step.
        ("x echo 'a #b'", 'Line 2: malformed command "echo \'a"'),
    ],
)
def test_exec_step_parse_errors(line: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        parse_plan(f"l\n{line}\n", STACK, pr_is_merged=lambda _n: False)


def test_exec_step_in_the_landed_prefix_is_assumed_completed() -> None:
    plan = parse_plan(
        "l 98\nx ./provision.sh\nl 99\nx ./smoke-test.sh\nl\n",
        STACK,
        pr_is_merged=lambda n: n in (98, 99),
    )
    assert [s.state for s in plan if isinstance(s, ExecStep)] == [
        "skipped",
        "pending",
    ]


def test_exec_step_replan_credit_needs_the_same_command_after_the_same_prs() -> None:
    old_stack = [StackEntry(pr_url="", pr_number=n, branch=f"b{n}") for n in (101, 102)]
    old_plan = parse_plan(
        "l\nx ./provision.sh\nx ./migrate.sh\nl\n",
        old_stack,
        pr_is_merged=lambda _n: False,
    )
    for step in old_plan[1:3]:
        assert isinstance(step, ExecStep)
        step.state = "succeeded"
    old_plan[2].state = "failed"  # type: ignore[attr-defined]
    old_stack[0].state = PRState.MERGED
    old = LandingContext(stack=old_stack, plan=old_plan, current_step=2)

    stack = old_stack[1:]
    plan = parse_plan(
        "l 101\nx ./migrate.sh\nx ./provision.sh\nx ./provision.sh --again\nl\n",
        stack,
        pr_is_merged=lambda n: n == 101,
    )
    lost = carry_over_progress(old, plan, stack)

    # Only a finished command carries over, even when reordered; a failed one,
    # or a changed command line, runs again.
    assert [s.state for s in plan if isinstance(s, ExecStep)] == [
        "pending",
        "succeeded",
        "pending",
    ]
    assert lost == []


@pytest.mark.parametrize(
    ("state", "outcome", "status", "tone"),
    [
        ("pending", StepOutcome.PENDING, "Pending", StepTone.MUTED),
        ("running", StepOutcome.ACTIVE, "Running", StepTone.RUNNING),
        ("succeeded", StepOutcome.DONE, "Succeeded", StepTone.SUCCESS),
        ("failed", StepOutcome.FAILED, "Failed", StepTone.FAILURE),
        ("skipped", StepOutcome.DONE, "Assumed completed", StepTone.SUCCESS),
    ],
)
def test_exec_view_follows_its_state(
    state: str, outcome: StepOutcome, status: str, tone: StepTone
) -> None:
    assert _view(ExecStep(command="./provision.sh", state=state)) == StepView(
        outcome=outcome,
        status=status,
        tone=tone,
        key="./provision.sh",
        description="Run ./provision.sh",
        data={"command": "./provision.sh"},
    )


def test_exec_view_detail_is_its_error() -> None:
    step = ExecStep(
        command="./provision.sh", state="failed", error_message="Command exited 1"
    )
    assert _view(step).detail == ("Command exited 1",)
