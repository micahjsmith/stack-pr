"""Where a PR in the stack is on its way to landing.

Its own module so the step types, which ``model`` imports, can use it too.
"""

from __future__ import annotations

from enum import Enum


class PRState(str, Enum):
    PENDING = "pending"
    WAITING_FOR_APPROVAL = "waiting_for_approval"
    WAITING_FOR_CHECKS = "waiting_for_checks"
    IN_MERGE_QUEUE = "in_merge_queue"
    WAITING_FOR_WORKFLOW = "waiting_for_workflow"
    MERGED = "merged"
    FAILED = "failed"
