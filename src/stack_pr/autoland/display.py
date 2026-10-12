"""Rendering the landing plan's progress for the terminal."""

from __future__ import annotations

import sys
from dataclasses import astuple, dataclass
from typing import Any

from stack_pr.autoland import runtime
from stack_pr.autoland.model import LandingContext, PlanStep
from stack_pr.autoland.plan import (
    generate_default_plan,
    is_assumed_completed,
    landed_prefix_end,
)
from stack_pr.autoland.runtime import Group, Padding, Text
from stack_pr.autoland.steps import StepOutcome, StepTone, StepView

# ---------------------------------------------------------------------------
# Status display
# ---------------------------------------------------------------------------

_REVIEW_DECISION_DISPLAY = {
    "APPROVED": ("Approved", "green"),
    "REVIEW_REQUIRED": ("Review required", "magenta"),
    "CHANGES_REQUESTED": ("Changes requested", "red"),
}


# Every plan line starts with an icon, so a plan scans as a column of glyphs
# before any of the words are read. Column alignment depends on the widths
# below holding for whichever set is in use, so both sets are fixed-width:
# _ICON_CELLS for the four outcome icons, _POINTER_CELLS for the pointer.
_POINTER_CELLS = 1
_ICON_CELLS = 2


@dataclass(frozen=True)
class _Glyphs:
    done: str
    active: str
    pending: str
    failed: str
    pointer: str  # marks the next step with work left to do
    detail: str  # bullet for a step's second line
    sep: str  # between the fragments of a headline or detail line


# The emoji are all East Asian Wide, so they occupy _ICON_CELLS on their own.
_UNICODE_GLYPHS = _Glyphs(
    done="✅",
    active="⏳",
    pending="⬜",
    failed="❌",
    pointer="→",
    detail="↳",
    sep=" · ",
)

# Used when stdout cannot encode the emoji. The status column names every state
# in words anyway, so the substitutes only have to be distinguishable.
_ASCII_GLYPHS = _Glyphs(
    done="OK",
    active="..",
    pending="  ",
    failed="!!",
    pointer=">",
    detail="*",
    sep=" - ",
)


def _pick_glyphs() -> _Glyphs:
    """The richest glyph set stdout can actually encode.

    Writing an un-encodable character raises UnicodeEncodeError, which would
    take down the whole landing run — print_status is called from the SIGINT
    handler and from _finish, so even a successful autoland would die on the
    way out, leaving its checkpoint and worktree behind.
    """
    encoding = getattr(sys.stdout, "encoding", None) or "ascii"
    try:
        "".join(astuple(_UNICODE_GLYPHS)).encode(encoding)
    except (UnicodeEncodeError, LookupError):
        return _ASCII_GLYPHS
    return _UNICODE_GLYPHS


GLYPHS = _pick_glyphs()


def _icon(outcome: StepOutcome) -> str:
    """The current glyph set's icon for *outcome*.

    Looked up per render rather than baked into the tables below, so a test can
    swap GLYPHS.
    """
    return str(getattr(GLYPHS, outcome.value))


# The color each kind of status is drawn in.
_TONE_STYLES = {
    StepTone.PLAIN: "",
    StepTone.MUTED: "dim",
    StepTone.SUCCESS: "green",
    StepTone.FAILURE: "red bold",
    StepTone.AWAITING_REVIEW: "magenta",
    StepTone.AWAITING_CHECKS: "yellow",
    StepTone.AWAITING_USER: "yellow",
    StepTone.IN_QUEUE: "cyan",
    StepTone.AWAITING_WORKFLOW: "blue",
}


@dataclass(frozen=True)
class _StepRow:
    """One plan step: its view, plus where it sits in the plan."""

    is_next: bool  # the first step with work left to do
    number: str  # "1.", "2." ...
    kind: str  # Land / Workflow / Confirm
    view: StepView

    @property
    def outcome(self) -> StepOutcome:
        return self.view.outcome

    @property
    def status(self) -> str:
        return self.view.status

    @property
    def status_style(self) -> str:
        return _TONE_STYLES[self.view.tone]

    @property
    def key(self) -> str:
        return self.view.key

    @property
    def note(self) -> str:
        return self.view.note

    @property
    def detail(self) -> str:
        """The second line: error message, retry counts, ... ("" for none)."""
        return GLYPHS.sep.join(self.view.detail)

    @property
    def done(self) -> bool:
        return self.outcome is StepOutcome.DONE


def _pointer_index(ctx: LandingContext, plan: list[PlanStep]) -> int:
    """Index of the step the pointer marks: the first with work left to do.

    A plan replayed against a partially-landed stack opens with a prefix of
    steps that are already satisfied (see ``landed_prefix_end``). Pointing at
    step 1 there reads as "about to redo work that shipped last week", so the
    pointer starts past the prefix. Once a run is under way ``current_step`` has
    moved further along and wins.
    """
    return max(ctx.current_step, landed_prefix_end(plan) + 1)


def _plan_rows(ctx: LandingContext) -> list[_StepRow]:
    plan = ctx.plan or generate_default_plan(ctx.stack)
    pointer = _pointer_index(ctx, plan)
    return [
        _StepRow(
            is_next=index == pointer,
            number=f"{index + 1}.",
            kind=step.label,
            view=step.view(
                ctx.stack,
                is_next=index == pointer,
                assumed_completed=is_assumed_completed(plan, index),
            ),
        )
        for index, step in enumerate(plan)
    ]


def _plan_headline(rows: list[_StepRow]) -> str:
    done = sum(1 for r in rows if r.done)
    noun = "step" if len(rows) == 1 else "steps"
    return GLYPHS.sep.join(
        [
            "Autoland plan",
            f"{len(rows)} {noun}",
            f"{done} done",
            f"{len(rows) - done} remaining",
        ]
    )


def _column_widths(rows: list[_StepRow]) -> tuple[int, int, int]:
    """Widths of the number, status, and kind columns."""
    return (
        max((len(r.number) for r in rows), default=0),
        max((len(r.status) for r in rows), default=0),
        max((len(r.kind) for r in rows), default=0),
    )


def _detail_indent(num_width: int) -> int:
    """Cell the detail line's bullet starts at: under the status column.

    Counted in terminal cells, not characters — the outcome icons are wide
    glyphs, so len() would undercount them and shift the line left.
    """
    return _POINTER_CELLS + 1 + _ICON_CELLS + 1 + num_width + 1


def _header_segments(
    row: _StepRow, widths: tuple[int, int, int]
) -> list[tuple[str, str]]:
    """A step's header line as (text, style) pairs.

    The one place the layout is defined; both renderers consume it, so the
    plain fallback cannot drift away from the rich output.
    """
    num_w, status_w, kind_w = widths
    dim = "dim " if row.done else ""
    segments = [
        (f"{GLYPHS.pointer if row.is_next else ' ':<{_POINTER_CELLS}} ", "bold cyan"),
        (f"{_icon(row.outcome)} ", ""),
        (f"{row.number:<{num_w}} ", f"{dim}bold"),
        (f"{row.status:<{status_w}}", row.status_style),
        (" | ", "dim"),
        (f"{row.kind:<{kind_w}}", f"{dim}bold"),
        (" | ", "dim"),
        (row.key, f"{dim}cyan"),
    ]
    if row.note:
        segments.append((f" - {row.note}", dim.strip()))
    return segments


def render_status_rich(ctx: LandingContext) -> Group:  # pragma: no cover
    """Render plan progress as compact, styled lines (rich-only path).

    Text is appended rather than parsed as markup, so a PR title containing
    something like "[1,25]" renders literally instead of being read as a style.
    """
    rows = _plan_rows(ctx)
    widths = _column_widths(rows)
    indent = _detail_indent(widths[0])

    # list[Any]: the rich renderable protocol isn't importable on the no-rich
    # path, and this function only runs when rich is present.
    parts: list[Any] = [Text(_plan_headline(rows), style="bold"), Text()]
    for row in rows:
        # Only the header line refuses to wrap: a long PR title would otherwise
        # fold back to column 0 and destroy the alignment the format is built
        # on, and the plan is a scannable overview — the full title is on the
        # PR. Detail and abort lines wrap normally, because a truncated error
        # message is the one thing a failed run cannot afford to lose.
        line = Text(no_wrap=True, overflow="ellipsis")
        for text, style in _header_segments(row, widths):
            line.append(text, style=style)
        parts.append(line)
        if row.detail:
            detail = Text(f"{GLYPHS.detail} {row.detail}", style="dim")
            # expand=False so the pad doesn't fill the rest of the line with
            # blanks; a wrapped detail still stays under the indent.
            parts.append(Padding(detail, (0, 0, 0, indent), expand=False))
    if ctx.aborted:
        parts += [Text(), Text(f"ABORTED: {ctx.abort_reason}", style="red bold")]
    return Group(*parts)


def render_status_plain(ctx: LandingContext) -> str:
    """Render plan progress as plain text (the no-rich fallback)."""
    rows = _plan_rows(ctx)
    widths = _column_widths(rows)
    indent = " " * _detail_indent(widths[0])

    lines = ["", _plan_headline(rows), ""]
    for row in rows:
        lines.append("".join(text for text, _ in _header_segments(row, widths)))
        if row.detail:
            lines.append(f"{indent}{GLYPHS.detail} {row.detail}")
    if ctx.aborted:
        lines += ["", f"ABORTED: {ctx.abort_reason}"]
    return "\n".join(lines)


def print_status(ctx: LandingContext) -> None:
    if runtime.HAVE_RICH:
        runtime.console.print(render_status_rich(ctx))
    else:
        runtime.console.print(render_status_plain(ctx))


def _escape_markup(text: str) -> str:
    """Escape rich markup so dynamic text (e.g. a PR title like '[1,25]') is
    rendered literally instead of parsed as a style tag."""
    if runtime.HAVE_RICH:
        from rich.markup import escape  # noqa: PLC0415

        return escape(text)
    return text


def _describe_step(step: PlanStep, ctx: LandingContext) -> str:
    """A one-line, human-readable description of a plan step.

    Prose for the confirm prompt's "Next steps" list. It comes from the same
    ``StepView`` the status panel shows, so the two cannot name a step
    differently.
    """
    return step.view(ctx.stack, is_next=False, assumed_completed=False).description
