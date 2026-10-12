"""Plan optimization: execution units and the policies that build them.

Importing this package imports every policy module, so each policy is in the
registry before a plan is executed.
"""

from __future__ import annotations

from stack_pr.autoland.optimize.base import (
    POLICIES,
    ExecutionUnit,
    OptimizationPolicy,
    PolicyRegistry,
    StepUnit,
    UnitContext,
    optimize_plan,
    register_policy,
)
from stack_pr.autoland.optimize.stack_merge import (
    NativeStackMergePolicy,
    StackMergeUnit,
)

__all__ = [
    "POLICIES",
    "ExecutionUnit",
    "NativeStackMergePolicy",
    "OptimizationPolicy",
    "PolicyRegistry",
    "StackMergeUnit",
    "StepUnit",
    "UnitContext",
    "optimize_plan",
    "register_policy",
]
