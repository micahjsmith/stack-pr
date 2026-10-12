"""How a run merges its PRs into the target branch: the merge queue, or directly.

The mode is resolved once per run, before anything lands, and carried in
``AutolandOptions.merge_mode``. Everything that merges reads it from there —
landing one PR at a time (``waits.merge_and_wait``) and merging a run as a
GitHub stack (``native_stack``) — so they always agree.
"""

from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from stack_pr.autoland.gh import GitHub


class MergeMode(str, Enum):
    QUEUE = "queue"  # add each PR to the target branch's merge queue
    DIRECT = "direct"  # squash-merge each PR straight into the target branch

    @property
    def uses_queue(self) -> bool:
        return self is MergeMode.QUEUE


class MergeModeError(Exception):
    """The merge mode could not be resolved."""


def resolve_merge_mode(
    target: str, github: GitHub, *, configured: bool | None
) -> MergeMode:
    """The run's merge mode.

    *configured* is ``autoland.merge_queue``: ``True`` or ``False`` forces the
    mode, and ``None`` (unset) asks GitHub whether *target* has a merge queue.
    Raises ``MergeModeError`` when GitHub can't say.
    """
    if configured is not None:
        return MergeMode.QUEUE if configured else MergeMode.DIRECT
    has_queue = github.has_merge_queue(target)
    if has_queue is None:
        raise MergeModeError(
            f"Could not tell whether {target} uses a merge queue. Say which "
            "with one of:\n"
            "    stack-pr config autoland.merge_queue=true\n"
            "    stack-pr config autoland.merge_queue=false"
        )
    return MergeMode.QUEUE if has_queue else MergeMode.DIRECT


def describe_merge_mode(mode: MergeMode, target: str) -> str:
    """One line saying how the run merges, for the start of its output."""
    if mode.uses_queue:
        return f"Merging into {target} through its merge queue"
    return f"Merging directly into {target} (no merge queue)"
