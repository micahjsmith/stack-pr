"""The plan step registry and the per-step-type plan syntax and persistence."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from stack_pr.autoland.model import LandingContext, StackEntry
from stack_pr.autoland.plan import format_plan_for_editor, parse_plan
from stack_pr.autoland.state import AutolandCheckpointer
from stack_pr.autoland.steps import (
    REGISTRY,
    AutolandStep,
    ConfirmStep,
    LandStep,
    PlanParser,
    StepRegistry,
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
}


def test_every_registered_step_type_has_a_sample() -> None:
    assert set(REGISTRY.types()) == set(SAMPLES)


@pytest.mark.parametrize(
    ("kind", "keyword", "step_type"),
    [
        ("land", "l", LandStep),
        ("workflow", "w", WorkflowStep),
        ("confirm", "c", ConfirmStep),
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
