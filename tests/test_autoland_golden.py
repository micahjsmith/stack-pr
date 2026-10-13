"""Golden-file characterization of whole autoland runs.

Each scenario runs `stack-pr autoland` end to end against a scripted GitHub
(see tests/autoland_scenarios.py) and compares what a user could observe with
the files in tests/golden/autoland/. Regenerate them with UPDATE_GOLDEN=1; see
the README there.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
from pytest_mock import MockerFixture

from stack_pr.autoland.model import ConfirmStep, LandingContext, PRState, WorkflowStep
from stack_pr.autoland.plan import parse_plan
from stack_pr.autoland.state import AutolandCheckpointer
from tests.autoland_scenarios import (
    BRANCH,
    FakePR,
    FakeWorld,
    Scenario,
    run_scenario,
    sha,
    stack_entries,
)

GOLDEN_DIR = Path(__file__).parent / "golden" / "autoland"
UPDATE = os.environ.get("UPDATE_GOLDEN") == "1"


def _prs(*numbers: int, **overrides: Any) -> list[FakePR]:
    titles = {
        101: "Add the widget model",
        102: "Expose widgets over the API",
        103: "Show widgets in the dashboard",
    }
    return [FakePR(n, titles[n], **overrides) for n in numbers]


# --- checkpoints of earlier runs -------------------------------------------


def _resume_checkpoint(
    world: FakeWorld, _plan_file: Path
) -> tuple[AutolandCheckpointer, LandingContext]:
    """A run started after #101 and #102 had landed (so the workflow and the
    confirmation between them are assumed done), stopped when the workflow
    after #102 timed out."""
    stack = stack_entries(world, [103])
    plan = parse_plan(
        "l 101\nw deploy.yaml\nc QA sign-off complete\nl 102\nw deploy.yaml\nl 103\n",
        stack,
        pr_is_merged=lambda n: n in (101, 102),
    )
    workflow = plan[4]
    assert isinstance(workflow, WorkflowStep)
    workflow.state = "failed"
    workflow.error_message = "Workflow timed out after 3h"
    ctx = LandingContext(
        stack=stack,
        plan=plan,
        current_step=4,
        abort_reason="Workflow deploy.yaml failed or timed out",
        last_landed_sha=sha("merge-102"),
    )
    return AutolandCheckpointer(path=Path(), branch=BRANCH, base="main"), ctx


_OLD_REPLAN_PLAN = """\
l
w deploy.yaml
c QA sign-off complete
l
w deploy.yaml
c metrics look healthy
c on-call agrees
l
"""


def _replan_checkpoint(
    world: FakeWorld, plan_file: Path
) -> tuple[AutolandCheckpointer, LandingContext]:
    """A run interrupted while landing #103, after #101 and #102 landed and
    every checkpoint between them was done."""
    stack = stack_entries(world, [101, 102, 103])
    plan = parse_plan(_OLD_REPLAN_PLAN, stack, pr_is_merged=lambda _n: False)
    for entry in stack[:2]:
        entry.state = PRState.MERGED
    stack[2].state = PRState.WAITING_FOR_CHECKS
    stack[2].error_message = "Checks in progress..."
    for step in plan[:7]:
        if isinstance(step, WorkflowStep):
            step.state = "succeeded"
        elif isinstance(step, ConfirmStep):
            step.confirmed = True
    ctx = LandingContext(
        stack=stack,
        plan=plan,
        current_step=7,
        current_index=2,
        abort_reason="User interrupted (Ctrl+C)",
        last_landed_sha=sha("merge-102"),
    )
    checkpointer = AutolandCheckpointer(
        path=Path(), branch=BRANCH, base="main", plan_file=plan_file
    )
    return checkpointer, ctx


# --- the scenarios -----------------------------------------------------------


def _one_at_a_time_prs() -> list[FakePR]:
    first, second = _prs(101, 102)
    first.checks = ["pending", "pass"]
    second.review, second.approve_after = "REVIEW_REQUIRED", 4
    return [first, second]


SCENARIOS = [
    # Two PRs landed one at a time, with a workflow and a confirmation between.
    Scenario(
        name="one_at_a_time",
        prs=_one_at_a_time_prs(),
        stack=[101, 102],
        plan="l\nw deploy.yaml\nc QA sign-off complete\nl\n",
        answers=["n", "y"],
        workflows={"deploy.yaml": "success"},
    ),
    # A run of two land steps merges as one GitHub stack; the land step after
    # the checkpoints is a run of one, so it lands on its own.
    Scenario(
        name="merge_as_stack",
        prs=_prs(101, 102, 103),
        stack=[101, 102, 103],
        plan="l\nl\nw deploy.yaml\nc\nl\n",
        config={"merge_as_stack": "true"},
        answers=["y"],
        workflows={"deploy.yaml": "success"},
    ),
    # The stack merge fails after landing only its bottom PR; the rest of the
    # run lands one PR at a time.
    Scenario(
        name="stack_merge_falls_through",
        prs=_prs(101, 102, 103),
        stack=[101, 102, 103],
        config={"merge_as_stack": "true"},
        stack_merge="fail",
        stack_merge_lands=[101],
        stack_merge_message="Merge conflict in PR #102",
    ),
    # Checks keep failing after their one rerun: the run stops at the first PR.
    Scenario(
        name="checks_fail",
        prs=[
            FakePR(101, "Add the widget model", checks=["fail", "pending", "fail"]),
            *_prs(102),
        ],
        stack=[101, 102],
        expect_exit=1,
    ),
    # Every deploy run fails, so the workflow checkpoint times out.
    Scenario(
        name="workflow_fails",
        prs=_prs(101, 102),
        stack=[101, 102],
        plan="l\nw deploy.yaml\nl\n",
        workflows={"deploy.yaml": "failure"},
        expect_exit=1,
    ),
    # A confirmation with no terminal to answer it.
    Scenario(
        name="confirm_eof",
        prs=_prs(101, 102),
        stack=[101, 102],
        plan="l\nc QA sign-off complete\nl\n",
        expect_exit=1,
    ),
    # --resume after a workflow timed out: waits for it again, then lands the
    # rest. The plan's already-landed prefix stays credited.
    Scenario(
        name="resume_mid_plan",
        prs=_prs(101, 102, 103),
        stack=[103],
        flags=["--resume"],
        workflows={"deploy.yaml": "success"},
        landed=[101, 102],
        checkpoint=_resume_checkpoint,
    ),
    # A fresh run of a plan whose first PRs already landed: the steps up to
    # the last of them are skipped, and assumed done.
    Scenario(
        name="landed_prefix",
        prs=_prs(101, 102, 103),
        stack=[103],
        landed=[101, 102],
        plan=(
            "l 101\nw deploy.yaml\nc QA sign-off complete\nl 102\n"
            "w deploy.yaml\nl 103\n"
        ),
        workflows={"deploy.yaml": "success"},
    ),
    # Commands between the land steps: the first succeeds, and the second
    # fails, which stops the run before the last PR.
    Scenario(
        name="exec_steps",
        prs=_prs(101, 102, 103),
        stack=[101, 102, 103],
        plan=(
            "l\nx ./scripts/provision.sh --env 'staging east'\nl\n"
            "x ./scripts/smoke-test.sh   # must pass before #103\nl\n"
        ),
        commands={
            "./scripts/provision.sh --env 'staging east'": 0,
            "./scripts/smoke-test.sh": 3,
        },
        expect_exit=1,
    ),
    # --replan with an edited plan file: finished checkpoints after the same
    # PRs carry over, a new one runs, and a removed one is reported.
    Scenario(
        name="replan_carry_over",
        prs=_prs(101, 102, 103),
        stack=[103],
        plan=(
            "l 101\nw deploy.yaml\nc QA sign-off complete\nl 102\n"
            "w deploy.yaml\nc metrics look healthy\nc docs updated\nl 103\n"
        ),
        flags=["--replan"],
        answers=["y", "y"],
        workflows={"deploy.yaml": "success"},
        landed=[101, 102],
        checkpoint=_replan_checkpoint,
    ),
    # No merge queue on the target branch, and none configured: autoland
    # detects that and merges each PR directly, with a workflow between.
    Scenario(
        name="direct_one_at_a_time",
        prs=_one_at_a_time_prs(),
        stack=[101, 102],
        plan="l\nw deploy.yaml\nl\n",
        config={"merge_queue": None},
        has_queue=False,
        workflows={"deploy.yaml": "success"},
    ),
    # A run of land steps merges as one GitHub stack, directly rather than
    # through a queue.
    Scenario(
        name="direct_merge_as_stack",
        prs=_prs(101, 102, 103),
        stack=[101, 102, 103],
        plan="l\nl\nw deploy.yaml\nl\n",
        config={"merge_queue": None, "merge_as_stack": "true"},
        has_queue=False,
        workflows={"deploy.yaml": "success"},
    ),
    # The first direct merge fails (the base moved under it); after approval
    # and checks are verified again, the retry goes through.
    Scenario(
        name="direct_merge_retry",
        prs=_prs(101, 102),
        stack=[101, 102],
        config={"merge_queue": None},
        has_queue=False,
        merge_failures=1,
    ),
    # GitHub can't say whether the target branch has a merge queue, and the
    # config doesn't either: autoland stops before doing anything.
    Scenario(
        name="merge_mode_unknown",
        prs=_prs(101, 102),
        stack=[101, 102],
        config={"merge_queue": None},
        queue_lookup_fails=True,
        expect_exit=1,
    ),
]


ARTIFACTS = {
    "out.txt": "out",
    "checkpoint.json": "checkpoint",
    "status.txt": "status_text",
    "status.json": "status_json",
    "plan.txt": "plan",
}


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.name)
def test_golden_scenario(
    scenario: Scenario,
    mocker: MockerFixture,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    result = run_scenario(
        scenario,
        mocker=mocker,
        monkeypatch=monkeypatch,
        capsys=capsys,
        tmp_path=tmp_path,
    )

    assert result.exit_code == scenario.expect_exit
    # The plan the editor shows parses back into the same plan.
    assert result.plan_round_trip == result.plan

    for suffix, attr in ARTIFACTS.items():
        path = GOLDEN_DIR / f"{scenario.name}.{suffix}"
        actual = getattr(result, attr)
        if actual is None:  # the run stopped before it wrote a checkpoint
            assert not path.exists(), f"{path.name} is stale; delete it"
            continue
        if UPDATE:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(actual)
            continue
        assert path.exists(), f"missing {path.name}; generate it with UPDATE_GOLDEN=1"
        assert actual == path.read_text(), (
            f"{path.name} differs; if the change is intended, regenerate with "
            "UPDATE_GOLDEN=1 and review the diff"
        )
