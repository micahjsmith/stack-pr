"""Plan optimization: the policy registry, the native-stack merge policy, and
the units policies build."""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from pathlib import Path

import pytest

from stack_pr.autoland import engine
from stack_pr.autoland.model import (
    ConfirmStep,
    LandingContext,
    LandStep,
    PRState,
    WorkflowStep,
)
from stack_pr.autoland.native_stack import NativeStackMergeResult
from stack_pr.autoland.optimize import (
    ExecutionUnit,
    PolicyRegistry,
    StackMergeUnit,
    StepUnit,
    UnitContext,
    optimize_plan,
)
from stack_pr.autoland.options import AutolandOptions
from stack_pr.autoland.state import AutolandCheckpointer
from stack_pr.autoland.steps import AutolandStep, StepResult
from tests.helpers import common_args
from tests.test_autoland_step_execution import OPTS, FakeUI, services, stack

STACKED = dataclasses.replace(OPTS, merge_as_stack=True)


def land(*indices: int) -> list[AutolandStep]:
    return [LandStep(entry_index=i) for i in indices]


def spans(units: list[ExecutionUnit]) -> list[tuple[str, int, int]]:
    """Each unit as (type, first step, last step)."""
    return [(type(u).__name__, u.indices.start, u.indices.stop - 1) for u in units]


# ---------------------------------------------------------------------------
# The policy registry
# ---------------------------------------------------------------------------


def _named(policy_name: str) -> type:
    class Policy:
        name = policy_name

        def applies(self, opts: AutolandOptions) -> bool:
            return True

        def optimize(
            self,
            units: list[ExecutionUnit],
            run: LandingContext,
            opts: AutolandOptions,
        ) -> list[ExecutionUnit]:
            return units

    return Policy


def test_policies_run_in_registration_order() -> None:
    registry = PolicyRegistry()
    registry.register(_named("b"))
    registry.register(_named("a"))

    assert [p.name for p in registry.policies()] == ["b", "a"]


def test_registering_a_policy_name_twice_is_an_error() -> None:
    registry = PolicyRegistry()
    registry.register(_named("a"))

    with pytest.raises(ValueError, match="'a' is already registered"):
        registry.register(_named("a"))


def test_a_policy_must_keep_every_step() -> None:
    class DropsSteps:
        name = "drops-steps"

        def applies(self, opts: AutolandOptions) -> bool:
            return True

        def optimize(
            self,
            units: list[ExecutionUnit],
            run: LandingContext,
            opts: AutolandOptions,
        ) -> list[ExecutionUnit]:
            return units[1:]

    registry = PolicyRegistry()
    registry.register(DropsSteps)
    run = LandingContext(stack=stack(101, 102), plan=land(0, 1))

    with pytest.raises(ValueError, match="'drops-steps'"):
        optimize_plan(run, OPTS, registry)


# ---------------------------------------------------------------------------
# The native-stack merge policy
# ---------------------------------------------------------------------------


def test_land_runs_are_split_by_workflow_and_confirm_steps() -> None:
    plan = [
        *land(0, 1),
        WorkflowStep(workflow="deploy.yaml"),
        *land(2),
        ConfirmStep(condition="QA"),
        *land(3, 4),
    ]
    run = LandingContext(stack=stack(101, 102, 103, 104, 105), plan=plan)

    units = optimize_plan(run, STACKED)

    # A lone 'l' between two checkpoints has nothing to merge alongside.
    assert spans(units) == [
        ("StackMergeUnit", 0, 1),
        ("StepUnit", 2, 2),
        ("StepUnit", 3, 3),
        ("StepUnit", 4, 4),
        ("StackMergeUnit", 5, 6),
    ]


def test_already_landed_and_merged_prs_are_left_out_of_runs() -> None:
    entries = stack(101, 102, 103, 104, 105)
    entries[0].state = PRState.MERGED
    entries[3].state = PRState.MERGED
    plan = [LandStep(entry_index=-1, pr_number=100), *land(0, 1, 2, 3, 4)]
    run = LandingContext(stack=entries, plan=plan)

    units = optimize_plan(run, STACKED)

    assert spans(units) == [
        ("StepUnit", 0, 0),
        ("StepUnit", 1, 1),
        ("StackMergeUnit", 2, 3),
        ("StepUnit", 4, 4),
        ("StepUnit", 5, 5),
    ]


def test_runs_start_at_the_current_step() -> None:
    run = LandingContext(stack=stack(101, 102, 103), plan=land(0, 1, 2))
    run.current_step = 1

    assert spans(optimize_plan(run, STACKED)) == [("StackMergeUnit", 1, 2)]


def test_merge_as_stack_off_leaves_every_step_on_its_own() -> None:
    run = LandingContext(stack=stack(101, 102), plan=land(0, 1))

    assert spans(optimize_plan(run, OPTS)) == [
        ("StepUnit", 0, 0),
        ("StepUnit", 1, 1),
    ]


def test_preview_announces_each_stack_merge(mocker) -> None:  # noqa: ANN001
    console = mocker.patch("stack_pr.autoland.runtime.console")
    plan = [*land(0, 1), WorkflowStep(workflow="deploy.yaml"), *land(2, 3)]
    run = LandingContext(stack=stack(101, 102, 103, 104), plan=plan)

    engine.print_execution_preview(run, STACKED)

    assert [c.args[0] for c in console.print.call_args_list] == [
        "[dim]Steps 1-2 (#101, #102) will merge together as a GitHub stack.[/dim]",
        "[dim]Steps 4-5 (#103, #104) will merge together as a GitHub stack.[/dim]",
    ]


# ---------------------------------------------------------------------------
# Executing a StackMergeUnit
# ---------------------------------------------------------------------------


class _Recorder:
    """A UnitContext's callbacks, recording what the unit does in order."""

    def __init__(self, run: LandingContext) -> None:
        self.run = run
        self.events: list[object] = []
        self.abort_at: int | None = None

    def context(self) -> UnitContext:
        return UnitContext(
            run=self.run,
            opts=STACKED,
            common=common_args(),
            services=services(),
            enter=lambda i: self.events.append(("enter", i)),
            execute_step=self.execute_step,
            checkpoint=lambda: self.events.append("checkpoint"),
        )

    def execute_step(self, index: int) -> StepResult:
        self.events.append(("step", index))
        if index == self.abort_at:
            return StepResult.abort(f"step {index} failed")
        return StepResult.done()


def _stack_merge(mocker, results: list[NativeStackMergeResult]):  # noqa: ANN001, ANN202
    """Patch the stack merge to return *results* in turn; return the mock."""
    return mocker.patch(
        "stack_pr.autoland.optimize.stack_merge.land_as_native_stack",
        side_effect=results,
    )


def _merged_prs(merge) -> list[list[int]]:  # noqa: ANN001
    return [[e.pr_number for e in c.args[0]] for c in merge.call_args_list]


def _unit(run: LandingContext) -> StackMergeUnit:
    (unit,) = optimize_plan(run, STACKED)
    assert isinstance(unit, StackMergeUnit)
    return unit


def test_stack_merge_then_each_step_executes(mocker) -> None:  # noqa: ANN001
    run = LandingContext(stack=stack(101, 102, 103), plan=land(0, 1, 2))

    def lands(entries, **_kw) -> NativeStackMergeResult:  # noqa: ANN001, ANN003
        for e in entries:
            e.state = PRState.MERGED
        return NativeStackMergeResult(landed=True)

    merge = mocker.patch(
        "stack_pr.autoland.optimize.stack_merge.land_as_native_stack",
        side_effect=lands,
    )
    recorder = _Recorder(run)

    assert _unit(run).execute(recorder.context()) == ""

    assert _merged_prs(merge) == [[101, 102, 103]]
    # Each step is entered (checkpointed as current) before any of its work.
    assert recorder.events == [
        ("enter", 0),
        "checkpoint",
        ("step", 0),
        ("enter", 1),
        ("step", 1),
        ("enter", 2),
        ("step", 2),
    ]


def test_a_stack_merge_that_falls_through_lands_the_rest_one_at_a_time(
    mocker,  # noqa: ANN001
) -> None:
    run = LandingContext(stack=stack(101, 102, 103), plan=land(0, 1, 2))
    merge = _stack_merge(mocker, [NativeStackMergeResult(landed=False)])
    recorder = _Recorder(run)

    assert _unit(run).execute(recorder.context()) == ""

    # The merge is not retried for the steps it covered.
    assert _merged_prs(merge) == [[101, 102, 103]]
    assert [e for e in recorder.events if e != "checkpoint"] == [
        ("enter", 0),
        ("step", 0),
        ("enter", 1),
        ("step", 1),
        ("enter", 2),
        ("step", 2),
    ]


def test_a_stack_merge_abort_stops_before_the_step(mocker) -> None:  # noqa: ANN001
    run = LandingContext(stack=stack(101, 102), plan=land(0, 1))
    _stack_merge(
        mocker,
        [NativeStackMergeResult(abort_reason="Rebase failed after merging #102: boom")],
    )
    recorder = _Recorder(run)

    assert (
        _unit(run).execute(recorder.context())
        == "Rebase failed after merging #102: boom"
    )
    assert recorder.events == [("enter", 0)]


def test_a_step_abort_stops_the_unit(mocker) -> None:  # noqa: ANN001
    run = LandingContext(stack=stack(101, 102, 103), plan=land(0, 1, 2))
    _stack_merge(mocker, [NativeStackMergeResult(landed=False)])
    recorder = _Recorder(run)
    recorder.abort_at = 1

    assert _unit(run).execute(recorder.context()) == "step 1 failed"
    assert ("enter", 2) not in recorder.events


def test_the_run_is_revalidated_when_the_unit_executes(mocker) -> None:  # noqa: ANN001
    run = LandingContext(stack=stack(101, 102, 103), plan=land(0, 1, 2))
    unit = _unit(run)
    # Merged elsewhere after the plan was optimized.
    run.stack[0].state = PRState.MERGED
    merge = _stack_merge(mocker, [NativeStackMergeResult(landed=True)])
    recorder = _Recorder(run)

    assert unit.execute(recorder.context()) == ""

    # Step 0 now only skips; the stack merge starts above it.
    assert _merged_prs(merge) == [[102, 103]]
    assert recorder.events[:4] == [
        ("enter", 0),
        ("step", 0),
        ("enter", 1),
        "checkpoint",
    ]


def test_a_run_shortened_to_one_pr_is_not_stack_merged(mocker) -> None:  # noqa: ANN001
    run = LandingContext(stack=stack(101, 102), plan=land(0, 1))
    unit = _unit(run)
    run.stack[1].state = PRState.MERGED
    merge = _stack_merge(mocker, [])
    recorder = _Recorder(run)

    assert unit.execute(recorder.context()) == ""

    merge.assert_not_called()
    assert recorder.events == [("enter", 0), ("step", 0), ("enter", 1), ("step", 1)]


# ---------------------------------------------------------------------------
# Adding a policy
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Pair(ExecutionUnit):
    """Two steps run back to back, announced together."""

    first: int
    log: list[str]

    @property
    def indices(self) -> range:
        return range(self.first, self.first + 2)

    def execute(self, uc: UnitContext) -> str:
        self.log.append(f"pair {self.first}")
        for index in self.indices:
            uc.enter(index)
            abort_reason = uc.execute_step(index).abort_reason
            if abort_reason:
                return abort_reason
        return ""

    def preview(self) -> list[str]:
        return [f"pair {self.first}"]


def test_a_policy_defined_outside_the_package_shapes_execution(
    tmp_path: Path,
    mocker,  # noqa: ANN001
) -> None:
    log: list[str] = []
    registry = PolicyRegistry()

    @registry.register
    class PairUp:
        name = "pair-up"

        def applies(self, opts: AutolandOptions) -> bool:
            return True

        def optimize(
            self,
            units: list[ExecutionUnit],
            run: LandingContext,
            opts: AutolandOptions,
        ) -> list[ExecutionUnit]:
            # Steps 0 and 1 become one unit; the rest stay as they are.
            assert isinstance(units[0], StepUnit)
            return [_Pair(first=0, log=log), *units[2:]]

    plan: list[AutolandStep] = [ConfirmStep(confirmed=True) for _ in range(3)]
    run = LandingContext(stack=stack(101), plan=plan)
    checkpointer = AutolandCheckpointer(
        path=tmp_path / "state.json", branch="feat", base="main"
    )
    saves = mocker.spy(checkpointer, "save")
    console = mocker.patch("stack_pr.autoland.runtime.console")

    engine.print_execution_preview(run, OPTS, policies=registry)
    assert engine.execute_plan(
        run,
        common_args(),
        OPTS,
        checkpointer,
        services=services(FakeUI()),
        policies=registry,
    )

    assert [c.args[0] for c in console.print.call_args_list] == ["pair 0"]
    assert log == ["pair 0"]
    assert run.current_step == len(plan)
    # One checkpoint as each step is entered, and one at the end.
    assert saves.call_count == len(plan) + 1
