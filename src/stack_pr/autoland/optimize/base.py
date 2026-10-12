"""Execution units, plan optimization policies, and the registry of policies.

The plan a user writes is a list of independent steps. Before executing it,
the engine turns the rest of the plan (from ``current_step``) into a list of
*execution units*: by default one ``StepUnit`` per step, which just executes
that step. Each registered ``OptimizationPolicy`` that applies to the run's
options may then replace a span of consecutive units with a composite unit
that does something across several steps (e.g. merge a run of PRs as one
GitHub stack).

Policies are where cross-step knowledge lives: a policy may look at step types
and at the run, while steps stay independent of each other and of policies.

The optimized plan is execution-time only. It is never persisted: the state
file holds the user's plan, ``current_step`` stays an index into it, and every
execution (fresh, resumed, or replanned) optimizes the plan again from
``current_step``. A unit runs its steps through ``UnitContext.enter`` and
``UnitContext.execute_step``, so each step is still checkpointed and executed
exactly as a ``StepUnit`` would.

To add a policy, define a class with a ``name``, ``applies(opts)``, and
``optimize(units, run, opts)``, decorate it with ``@register_policy``, and
import its module from ``optimize/__init__.py``. Policies run in registration
order.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, TypeVar

if TYPE_CHECKING:
    from stack_pr import cli
    from stack_pr.autoland.model import LandingContext
    from stack_pr.autoland.options import AutolandOptions
    from stack_pr.autoland.services import Services
    from stack_pr.autoland.steps import StepResult


@dataclass(frozen=True)
class UnitContext:
    """What an execution unit is given to run its steps with."""

    run: LandingContext
    opts: AutolandOptions
    common: cli.CommonArgs
    services: Services
    # Make plan step *index* the current step and checkpoint, so a resume
    # starts from it. Call before doing any of the step's work.
    enter: Callable[[int], None]
    # Execute plan step *index* (after ``enter``) and record the run-level
    # facts it reports (its landed SHA). Returns the step's result.
    execute_step: Callable[[int], StepResult]
    # Save the run's state now.
    checkpoint: Callable[[], None]


class ExecutionUnit(ABC):
    """A span of consecutive plan steps executed together."""

    @property
    @abstractmethod
    def indices(self) -> range:
        """The plan step indices the unit covers: consecutive and non-empty."""

    @abstractmethod
    def execute(self, uc: UnitContext) -> str:
        """Run the unit's steps, in order. Returns the reason the plan must
        stop, or "" once every step is done."""

    def preview(self) -> list[str]:
        """Markup lines announcing, before execution starts, what the unit
        does beyond executing its steps one by one."""
        return []


@dataclass(frozen=True)
class StepUnit(ExecutionUnit):
    """The default unit: plan step ``index``, executed on its own."""

    index: int

    @property
    def indices(self) -> range:
        return range(self.index, self.index + 1)

    def execute(self, uc: UnitContext) -> str:
        uc.enter(self.index)
        return uc.execute_step(self.index).abort_reason


class OptimizationPolicy(Protocol):
    """Rewrites the execution plan, replacing spans of units with composites."""

    # Unique among registered policies.
    name: str

    def applies(self, opts: AutolandOptions) -> bool:
        """Whether the policy is on for a run with *opts*."""
        ...

    def optimize(
        self,
        units: list[ExecutionUnit],
        run: LandingContext,
        opts: AutolandOptions,
    ) -> list[ExecutionUnit]:
        """The units to execute instead of *units*.

        The result must cover the same plan steps, in order, each exactly once.
        Must not change *run*.
        """
        ...


_P = TypeVar("_P", bound=OptimizationPolicy)


class PolicyRegistry:
    """The optimization policies, in the order they run."""

    def __init__(self) -> None:
        self._policies: dict[str, OptimizationPolicy] = {}

    def register(self, cls: type[_P]) -> type[_P]:
        """Add an instance of *cls* after the policies already registered.

        Raises ValueError if a policy with its name is already registered.
        """
        policy = cls()
        if policy.name in self._policies:
            raise ValueError(
                f"Optimization policy {policy.name!r} is already registered by "
                f"{type(self._policies[policy.name]).__name__}"
            )
        self._policies[policy.name] = policy
        return cls

    def policies(self) -> list[OptimizationPolicy]:
        """Every registered policy, in registration order."""
        return list(self._policies.values())


POLICIES = PolicyRegistry()


def register_policy(cls: type[_P]) -> type[_P]:
    """Class decorator adding an optimization policy to the registry."""
    return POLICIES.register(cls)


def optimize_plan(
    run: LandingContext,
    opts: AutolandOptions,
    policies: PolicyRegistry | None = None,
) -> list[ExecutionUnit]:
    """The execution units for the rest of the plan, from ``run.current_step``.

    Starts from one ``StepUnit`` per step and applies every policy that applies
    to *opts*, in registration order. Raises ValueError if a policy returns
    units that don't cover the plan's remaining steps in order.
    """
    if policies is None:
        policies = POLICIES
    steps = range(run.current_step, len(run.plan))
    units: list[ExecutionUnit] = [StepUnit(i) for i in steps]
    for policy in policies.policies():
        if policy.applies(opts):
            units = policy.optimize(list(units), run, opts)
            covered = [i for unit in units for i in unit.indices]
            if covered != list(steps) or not all(unit.indices for unit in units):
                raise ValueError(
                    f"Optimization policy {policy.name!r} returned units that "
                    "do not cover the remaining plan steps in order"
                )
    return units
