"""Landing plans: generating, formatting, parsing, editing, and replanning them."""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

from stack_pr.autoland import gh, runtime
from stack_pr.autoland.model import (
    LandingContext,
    LandStep,
    PlanStep,
    StackEntry,
    WorkflowStep,
)
from stack_pr.autoland.steps import REGISTRY, PlanParser, ReplanKey
from stack_pr.shell_commands import run_shell_command

# ---------------------------------------------------------------------------
# Interactive plan editing
# ---------------------------------------------------------------------------


def generate_default_plan(
    stack: list[StackEntry],
    default_workflow: str | None = None,
    count: int | None = None,
) -> list[PlanStep]:
    # Land the bottom `count` PRs (the whole stack when count is None). Landing
    # goes bottom-to-top, so a partial land is always a prefix of the stack.
    n = len(stack) if count is None else count
    plan: list[PlanStep] = [
        LandStep(entry_index=i, pr_number=stack[i].pr_number) for i in range(n)
    ]
    # If a default workflow is configured, wait for it once those PRs have
    # landed. The user can still edit or remove this step in interactive mode.
    if default_workflow:
        plan.append(WorkflowStep(workflow=default_workflow))
    return plan


# Where the comments in a generated plan start, and how far past a step that
# reaches into that column they move instead. Kept in sync with COMMENT_COLUMN
# and COMMENT_GAP in editors/vscode/src/format.js, which formats plans the same
# way, so a generated plan is already formatted.
PLAN_COMMENT_COLUMN = 30
PLAN_COMMENT_GAP = 4


def format_plan_for_editor(stack: list[StackEntry], plan: list[PlanStep]) -> str:
    lines = [
        "# Autoland plan — edit steps below.",
        "# l [<pr>]        = land that PR (a number, or its URL); a bare 'l'",
        "#                   lands the next PR in the stack instead",
        "# w <workflow>    = wait for a workflow to complete",
        "# c [<condition>] = pause for manual confirmation; the optional",
        "#                   condition names what to verify before proceeding",
        "#                   (e.g. 'c QA sign-off complete')",
        "# x <command>     = run a command, and stop unless it exits 0; it runs",
        "#                   without a shell (use 'x bash -c ...' for pipes)",
        "#",
        "# Lines starting with # are comments and are ignored.",
        "# Blank lines are ignored.",
        "#",
    ]

    # Each step as (aligns comments?, line, trailing comment), so the comments
    # can be aligned once the longest step is known. The steps are emitted as
    # one contiguous block, which is the unit comments are aligned within.
    steps = [(step.aligns_comments, *step.plan_line(stack)) for step in plan]

    # Only some steps decide the column (see AutolandStep.aligns_comments): a
    # confirm condition is free text that routinely runs long, and letting it
    # decide would drag every comment off to the right.
    longest = max((len(line) for aligns, line, _ in steps if aligns), default=0)
    column = max(PLAN_COMMENT_COLUMN, longest + PLAN_COMMENT_GAP)
    lines.extend(
        f"{line}{' ' * max(column - len(line), 1)}# {comment}" if comment else line
        for _aligns, line, comment in steps
    )

    lines.append("")
    return "\n".join(lines)


def _pr_is_merged(pr_number: int) -> bool:
    """Whether GitHub reports *pr_number* as merged."""
    try:
        return gh.github.pr_state(pr_number) == "MERGED"
    except RuntimeError as e:
        raise ValueError(f"Could not look up PR #{pr_number} on GitHub: {e}") from e


def parse_plan(
    text: str,
    stack: list[StackEntry],
    *,
    pr_is_merged: Callable[[int], bool] = _pr_is_merged,
) -> list[PlanStep]:
    """Parse an edited plan back into steps. Raises ValueError if malformed.

    An ``l`` step may pin the PR it lands (``l 123`` or ``l <pr url>``) instead
    of taking the next PR in the stack positionally. A pinned PR that has
    already landed is no longer in the stack; such a step resolves to
    ``entry_index=-1`` and is skipped at execution time, so one plan file stays
    valid as the stack lands piece by piece. *pr_is_merged* checks that claim
    against GitHub and is injectable for testing.
    """
    steps: list[PlanStep] = []
    parser = PlanParser(stack=stack, pr_is_merged=pr_is_merged)

    for line_num, raw_line in enumerate(text.splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if " #" in line:
            line = line[: line.index(" #")].strip()

        # A step is its keyword, then (after a space) its argument, if any.
        keyword, sep, args = line.partition(" ")
        step_type = REGISTRY.for_keyword(keyword)
        if step_type is None or (not sep and not step_type.bare_keyword_ok):
            raise ValueError(f"Line {line_num}: unrecognized step: {raw_line!r}")
        parser.line_num = line_num
        steps.append(step_type.parse(args.strip(), parser))

    # A partial land is allowed (land only the bottom N PRs), but the plan must
    # land at least one PR. Because 'l' steps take stack entries bottom-to-top,
    # the landed PRs are always a prefix of the stack; the rest stay open.
    if not any(isinstance(step, LandStep) for step in steps):
        raise ValueError("Plan has no 'l' steps — nothing to land")

    # Everything before the last already-landed 'l' step happened in an earlier
    # run: the workflows ran and the confirmations were given before those PRs
    # could have merged. Mark them done so a re-run picks up where it left off
    # rather than re-prompting for sign-off on work that already shipped.
    for index, step in enumerate(steps):
        if is_assumed_completed(steps, index):
            step.assume_completed()

    return steps


def landed_prefix_end(plan: list[PlanStep]) -> int:
    """Index of the last ``l`` step whose PR had already landed, or -1 for none.

    Every step at or before this index is complete: the PR merged, and the
    workflow/confirm/command steps ahead of it must have run before it could. This is
    the boundary ``execute_plan`` resumes from and the display points at.

    Prefer ``is_assumed_completed`` for "is this step inside the prefix?" — the
    -1 makes that comparison work out on its own, but it slices from the *end*
    of a list if it is ever used as a bound directly.
    """
    return max((i for i, s in enumerate(plan) if s.marks_landed_prefix), default=-1)


def is_assumed_completed(plan: list[PlanStep], index: int) -> bool:
    """Whether the step at *index* sits inside the plan's already-landed prefix.

    Such a step was never run in this session; it is credited as done because
    the PRs after it could not have merged otherwise.
    """
    return index < landed_prefix_end(plan)


# ---------------------------------------------------------------------------
# Replanning: carry a previous run's progress over to a new plan
# ---------------------------------------------------------------------------


def _checkpoint_keys(
    plan: list[PlanStep], stack: list[StackEntry]
) -> list[ReplanKey | None]:
    """Each step's ``ReplanKey`` (see ``AutolandStep.replan_key``), or None."""
    landed: set[int] = set()
    keys: list[ReplanKey | None] = []
    for step in plan:
        keys.append(step.replan_key(frozenset(landed)))
        pr = step.landed_pr(stack)
        if pr is not None:
            landed.add(pr)
    return keys


def carry_over_progress(
    old: LandingContext, plan: list[PlanStep], stack: list[StackEntry]
) -> list[int]:
    """Credit *plan*'s workflow, confirm, and command steps with results from
    *old*.

    *plan* is a freshly parsed plan for the current *stack*. Land steps need no
    help: whether a PR merged is GitHub's to say, and parsing already asked.
    A workflow, confirm, or command step is credited when *old* completed a
    step with the same kind, text, and set of PRs landed before it — so adding,
    removing, or reordering steps keeps credit, while changing a step's text,
    or landing a different set of PRs ahead of it, makes it run again.

    Returns the indices (into ``old.plan``) of completed checkpoints that found
    no match in *plan*, so the caller can show what does not carry over.
    """
    unmatched: dict[ReplanKey, list[int]] = {}
    for index, key in enumerate(_checkpoint_keys(old.plan, old.stack)):
        if key is not None and old.plan[index].is_checkpoint_done():
            unmatched.setdefault(key, []).append(index)

    for step, key in zip(plan, _checkpoint_keys(plan, stack)):
        if key is None or not unmatched.get(key):
            continue
        previous = old.plan[unmatched[key].pop(0)]
        # A step parsing already credited (inside the landed prefix) keeps that
        # credit, but still consumes its match so it isn't reported as lost.
        if not step.is_checkpoint_done():
            step.credit_from(previous)

    return sorted(i for indices in unmatched.values() for i in indices)


# The conventional suffix for a landing plan. Plans autoland writes carry it,
# and editors key off it (see editors/vscode) to highlight the plan syntax.
PLAN_SUFFIX = ".autoland-plan"


def plan_from_file(path: Path, stack: list[StackEntry]) -> list[PlanStep]:
    """Load a landing plan from a file.

    The file uses the exact same format as the interactive editor (see
    ``format_plan_for_editor``): ``l [pr]`` / ``w <workflow>`` /
    ``c [condition]`` / ``x <command>`` steps, with ``#`` comments and blank
    lines ignored. It is parsed by the same ``parse_plan`` the editor uses, so
    a file saved from ``-i`` (or written by hand in that format) round-trips.
    """
    try:
        text = path.read_text()
    except OSError as e:
        runtime.console.print(f"[red]Could not read plan file {path}: {e}[/red]")
        sys.exit(1)
    try:
        return parse_plan(text, stack)
    except ValueError as e:
        runtime.console.print(f"[red]Invalid plan in {path}: {e}[/red]")
        sys.exit(1)


def edit_plan_interactive(
    stack: list[StackEntry],
    default_workflow: str | None = None,
    count: int | None = None,
    *,
    initial_text: str | None = None,
) -> list[PlanStep]:
    """Open a plan in $EDITOR and return the parsed result.

    The editor starts from *initial_text* when given (``--replan -i`` passes the
    plan being replaced), and from the default plan otherwise.
    """
    plan_text = initial_text or format_plan_for_editor(
        stack, generate_default_plan(stack, default_workflow, count)
    )
    editor = os.environ.get("EDITOR", "vim")

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=PLAN_SUFFIX, prefix="autoland-plan-", delete=False
    ) as f:
        f.write(plan_text)
        plan_file = f.name

    try:
        runtime.console.print(f"[bold]Opening plan in {editor}...[/bold]")
        # $EDITOR is a command line, not just a program name ("code --wait").
        # An empty value splits to nothing; keep it so the error names it.
        run_shell_command(
            [*(shlex.split(editor) or [editor]), plan_file], quiet=False, check=True
        )
        edited_text = Path(plan_file).read_text()

        non_comment = [
            ln.strip()
            for ln in edited_text.splitlines()
            if ln.strip() and not ln.strip().startswith("#")
        ]
        if not non_comment:
            runtime.console.print("[yellow]Empty plan — aborting.[/yellow]")
            sys.exit(0)

        return parse_plan(edited_text, stack)
    except ValueError as e:
        runtime.console.print(f"[red]Invalid plan: {e}[/red]")
        sys.exit(1)
    except subprocess.CalledProcessError:
        runtime.console.print(f"[red]Editor ({editor}) exited with an error.[/red]")
        sys.exit(1)
    finally:
        Path(plan_file).unlink(missing_ok=True)
