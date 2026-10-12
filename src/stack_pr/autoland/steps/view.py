"""What a step reports about itself for display: ``StepView``.

A step decides what it has to say — its outcome, status, subject, and any
detail — and ``display`` decides how that looks: icons, colors, and layout.
Nothing here names a glyph or a style, so a step type never has to know how
it is drawn, and the display never has to know which type it is drawing.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class StepOutcome(Enum):
    """The broad state a step is in, independent of how it is drawn.

    The values appear in ``autoland --status -o json``.
    """

    DONE = "done"
    ACTIVE = "active"
    PENDING = "pending"
    FAILED = "failed"


class StepTone(Enum):
    """What kind of status a step is reporting, for the display to color.

    Finer than ``StepOutcome``: the active tones say what the step is waiting
    on, so the display can tell a PR waiting for review from one waiting for
    CI at a glance.
    """

    PLAIN = "plain"  # no emphasis either way
    MUTED = "muted"  # nothing has happened yet
    SUCCESS = "success"
    FAILURE = "failure"
    AWAITING_REVIEW = "awaiting_review"
    AWAITING_CHECKS = "awaiting_checks"
    AWAITING_USER = "awaiting_user"
    IN_QUEUE = "in_queue"
    MERGING = "merging"  # being merged directly, with no merge queue
    AWAITING_WORKFLOW = "awaiting_workflow"
    RUNNING = "running"  # running a command of its own


@dataclass(frozen=True)
class StepView:
    """One step's state as content: everything the display shows about it."""

    outcome: StepOutcome
    # The status column, e.g. "Waiting for checks" or "Assumed completed".
    status: str
    tone: StepTone
    # The step's subject: "#103", "deploy.yaml", the confirm condition.
    key: str
    # One line of prose naming the step, for lists such as "Next steps".
    description: str
    # Says more about the subject (a PR's title); "" when there is nothing.
    note: str = ""
    # Fragments of the step's second line (an error, retry counts); the
    # display decides how to join them. Empty when the header says it all.
    detail: tuple[str, ...] = ()
    # Type-specific fields for ``autoland --status -o json``.
    data: Mapping[str, Any] = field(default_factory=dict)
