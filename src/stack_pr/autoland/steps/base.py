"""The base class every plan step derives from, and the registry of step types.

A step type owns everything that differs by kind of step: how it is spelled in
a plan file, how it is stored in the state file, how it counts toward a run's
progress, what it reports about itself for display (a ``StepView``), and how
it executes (given a ``StepContext``, returning a ``StepResult``). Code
that walks a plan calls these methods instead of switching on the step's type,
so adding a step type means adding a class here rather than a branch in every
module that handles plans.
"""

from __future__ import annotations

import dataclasses
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar, TypeVar

if TYPE_CHECKING:
    from stack_pr.autoland.model import StackEntry
    from stack_pr.autoland.steps.execution import StepContext, StepResult
    from stack_pr.autoland.steps.view import StepView

# A workflow, confirm, or command step's identity for replanning: its keyword, its text,
# and the PRs landed before it. A step means "once these PRs have landed, this
# holds", so a result recorded under the same PRs is still true in a new plan.
ReplanKey = tuple[str, str, frozenset[int]]

_S = TypeVar("_S", bound="AutolandStep")
_T = TypeVar("_T", bound="type[AutolandStep]")


@dataclass
class PlanParser:
    """What parsing a plan line needs beyond the line itself.

    One parser is shared by every line of a plan, so a step type can keep
    state across lines in it (``next_index``, advanced by each live ``l``).
    """

    stack: list[StackEntry]
    # Whether GitHub reports a PR as merged; injectable for testing.
    pr_is_merged: Callable[[int], bool]
    # The next stack entry a positional 'l' step may claim. Landing goes
    # bottom-to-top, so live land steps take stack entries in order from 0.
    next_index: int = 0
    # The 1-based line being parsed, for error messages.
    line_num: int = 0


@dataclass
class AutolandStep(ABC):
    """One step of a landing plan.

    Subclasses are dataclasses whose fields are the step's persisted state;
    the class attributes below identify the type.
    """

    # The type tag in the state file.
    kind: ClassVar[str]
    # The keyword that starts the step's line in a plan.
    keyword: ClassVar[str]
    # The step type's human-readable name.
    label: ClassVar[str]
    # Whether the step's plan line decides where plan comments are aligned.
    aligns_comments: ClassVar[bool] = True
    # Whether the keyword alone, with no argument, is a valid plan line.
    bare_keyword_ok: ClassVar[bool] = True

    # --- plan syntax ---------------------------------------------------------

    @classmethod
    @abstractmethod
    def parse(cls: type[_S], args: str, parser: PlanParser) -> _S:
        """Build the step from the text after its keyword (stripped; may be
        empty). Raises ValueError, naming ``parser.line_num``, if malformed."""

    @abstractmethod
    def plan_line(self, stack: list[StackEntry]) -> tuple[str, str]:
        """The step as a plan line, and a trailing comment ("" for none)."""

    # --- persistence ---------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """The step as stored in the state file: its type tag and its fields."""
        return {"type": self.kind, **dataclasses.asdict(self)}

    @classmethod
    def from_dict(cls: type[_S], data: dict[str, Any]) -> _S:
        """Restore a step written by ``to_dict``."""
        fields = {k: v for k, v in data.items() if k != "type"}
        step: _S = cls(**fields)
        return step

    # --- progress ------------------------------------------------------------

    @property
    def marks_landed_prefix(self) -> bool:
        """Whether the plan up to and including this step already happened."""
        return False

    def landed_pr(self, stack: list[StackEntry]) -> int | None:
        """The PR that has landed once this step is done, if any."""
        return None

    def assume_completed(self) -> None:
        """Mark the step as satisfied by an earlier, partial run."""

    def is_checkpoint_done(self) -> bool:
        """Whether the step reached a result a replanned run may reuse."""
        return False

    def replan_key(self, landed_prs: frozenset[int]) -> ReplanKey | None:
        """The step's identity for replanning, given the PRs landed before it.

        None for a step whose progress never carries over to a new plan.
        """
        return None

    def credit_from(self, old: AutolandStep) -> None:
        """Record on this step the result *old*, its match in the previous
        run's plan, reached."""

    # --- display -------------------------------------------------------------

    @abstractmethod
    def view(
        self, stack: list[StackEntry], *, is_next: bool, assumed_completed: bool
    ) -> StepView:
        """What the step reports about itself: status, subject, and detail.

        ``is_next`` is whether this is the next step with work left to do, and
        ``assumed_completed`` whether it sits inside the plan's already-landed
        prefix (see ``plan.is_assumed_completed``).
        """

    # --- execution -----------------------------------------------------------

    @abstractmethod
    def execute(self, sc: StepContext) -> StepResult:
        """Do the step's work, or nothing if it is already done.

        A step is independent of every other: it sees the run only through
        *sc*, and uses only ``sc.services`` to reach the outside world.
        """


class StepRegistry:
    """The step types, looked up by state-file kind or by plan keyword."""

    def __init__(self) -> None:
        self._by_kind: dict[str, type[AutolandStep]] = {}
        self._by_keyword: dict[str, type[AutolandStep]] = {}

    def register(self, cls: _T) -> _T:
        """Add *cls*. Raises ValueError if its kind or keyword is taken."""
        if cls.kind in self._by_kind:
            raise ValueError(
                f"Step kind {cls.kind!r} is already registered by "
                f"{self._by_kind[cls.kind].__name__}"
            )
        if cls.keyword in self._by_keyword:
            raise ValueError(
                f"Step keyword {cls.keyword!r} is already registered by "
                f"{self._by_keyword[cls.keyword].__name__}"
            )
        self._by_kind[cls.kind] = cls
        self._by_keyword[cls.keyword] = cls
        return cls

    def for_kind(self, kind: str) -> type[AutolandStep]:
        """The step type stored under *kind*. Raises ValueError if unknown."""
        try:
            return self._by_kind[kind]
        except KeyError:
            raise ValueError(f"Unknown plan step type: {kind!r}") from None

    def for_keyword(self, keyword: str) -> type[AutolandStep] | None:
        """The step type spelled *keyword* in a plan, or None if there is none."""
        return self._by_keyword.get(keyword)

    def types(self) -> list[type[AutolandStep]]:
        """Every registered step type, in registration order."""
        return list(self._by_kind.values())


REGISTRY = StepRegistry()


def register_step(cls: _T) -> _T:
    """Class decorator adding a step type to the registry."""
    return REGISTRY.register(cls)


def step_from_dict(data: dict[str, Any]) -> AutolandStep:
    """Restore a step from the state file. Raises ValueError for an unknown type."""
    return REGISTRY.for_kind(data["type"]).from_dict(data)
