"""Landing plans: generating, formatting, parsing, editing, and replanning them."""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

from stack_pr.autoland import gh, runtime
from stack_pr.autoland.model import (
    ConfirmStep,
    LandingContext,
    LandStep,
    PlanStep,
    StackEntry,
    WorkflowStep,
)
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
        "#",
        "# Lines starting with # are comments and are ignored.",
        "# Blank lines are ignored.",
        "#",
    ]

    # Each step as (keyword, line, trailing comment), so the comments can be
    # aligned once the longest step is known. The steps are emitted as one
    # contiguous block, which is the unit comments are aligned within.
    steps: list[tuple[str, str, str]] = []
    for step in plan:
        if isinstance(step, LandStep):
            if step.already_landed:
                steps.append(("l", f"l {step.pr_number}", "already landed"))
                continue
            entry = stack[step.entry_index]
            steps.append(("l", f"l {entry.pr_number}", entry.title or ""))
        elif isinstance(step, WorkflowStep):
            steps.append(("w", f"w {step.workflow}", ""))
        elif isinstance(step, ConfirmStep):
            steps.append(("c", f"c {step.condition}".rstrip(), ""))

    # Only 'l' and 'w' steps decide the column: a confirm condition is free
    # text that routinely runs long, and letting it decide would drag every
    # comment off to the right.
    longest = max((len(line) for kw, line, _ in steps if kw in ("l", "w")), default=0)
    column = max(PLAN_COMMENT_COLUMN, longest + PLAN_COMMENT_GAP)
    lines.extend(
        f"{line}{' ' * max(column - len(line), 1)}# {comment}" if comment else line
        for _kw, line, comment in steps
    )

    lines.append("")
    return "\n".join(lines)


# A pinned PR reference: a bare number, or the PR's URL. The owner and repo are
# captured so a URL can be checked against the repo being landed into. Note that
# the '#123' spelling is deliberately unsupported: '#' starts a comment, so
# 'l #123' is indistinguishable from a bare 'l' with a comment after it.
RE_PR_URL = re.compile(r"^https?://[^/]+/([^/]+)/([^/]+)/pull/(\d+)/?$")


def _current_owner_repo() -> tuple[str, str]:
    """The repository autoland is landing into, as ``(owner, name)``."""
    try:
        return gh.github.owner_repo()
    except (RuntimeError, KeyError) as e:
        raise ValueError(f"Could not determine the current repository: {e}") from e


def _parse_pr_ref(ref: str, line_num: int) -> int:
    """Parse the argument of an ``l`` step into a PR number.

    A URL must point at the repository being landed into. Everything downstream
    — the merge check, the merge queue, the stack itself — is resolved against
    that single repo, so a PR elsewhere could not be landed even if its number
    were understood; saying so beats silently landing whatever PR happens to
    carry the same number here.
    """
    m = RE_PR_URL.match(ref)
    if m:
        owner, repo, number = m.groups()
        this_owner, this_repo = _current_owner_repo()
        # GitHub treats owner/repo case-insensitively, so a URL that differs
        # only in case still points at this repo.
        if (owner.lower(), repo.lower()) != (this_owner.lower(), this_repo.lower()):
            raise ValueError(
                f"Line {line_num}: {ref} points at {owner}/{repo}, but this "
                f"stack lands into {this_owner}/{this_repo} — landing PRs "
                "across repositories is not currently supported"
            )
        return int(number)
    if ref.isdigit():
        return int(ref)
    raise ValueError(
        f"Line {line_num}: 'l' takes a PR number or URL, not {ref!r} "
        "(use a bare 'l' to land the next PR in the stack)"
    )


def _pr_is_merged(pr_number: int) -> bool:
    """Whether GitHub reports *pr_number* as merged."""
    try:
        return gh.github.pr_state(pr_number) == "MERGED"
    except RuntimeError as e:
        raise ValueError(f"Could not look up PR #{pr_number} on GitHub: {e}") from e


def _mark_skipped(step: PlanStep) -> None:
    """Mark a step as already satisfied by an earlier, partial run."""
    if isinstance(step, WorkflowStep):
        step.state = "skipped"
    elif isinstance(step, ConfirmStep):
        step.confirmed = True


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
    # The next stack entry an 'l' step may claim. Landing goes bottom-to-top,
    # so live land steps must take stack entries in order, starting at 0.
    next_index = 0
    land_steps = 0

    for line_num, raw_line in enumerate(text.splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if " #" in line:
            line = line[: line.index(" #")].strip()

        if line == "l" or line.startswith("l "):
            ref = line[2:].strip() if line.startswith("l ") else ""
            land = _resolve_land_step(
                ref,
                line_num,
                stack=stack,
                next_index=next_index,
                pr_is_merged=pr_is_merged,
            )
            steps.append(land)
            land_steps += 1
            # An already-landed step claims no stack entry, so the next live
            # 'l' still expects the PR currently at the bottom of the stack.
            if not land.already_landed:
                next_index += 1
        elif line.startswith("w "):
            workflow = line[2:].strip()
            if not workflow:
                raise ValueError(f"Line {line_num}: 'w' requires a workflow name")
            steps.append(WorkflowStep(workflow=workflow))
        elif line == "c" or line.startswith("c "):
            # The condition is optional: a bare 'c' just pauses to confirm.
            condition = line[2:].strip() if line.startswith("c ") else ""
            steps.append(ConfirmStep(condition=condition))
        else:
            raise ValueError(f"Line {line_num}: unrecognized step: {raw_line!r}")

    # A partial land is allowed (land only the bottom N PRs), but the plan must
    # land at least one PR. Because 'l' steps take stack entries bottom-to-top,
    # the landed PRs are always a prefix of the stack; the rest stay open.
    if land_steps == 0:
        raise ValueError("Plan has no 'l' steps — nothing to land")

    # Everything before the last already-landed 'l' step happened in an earlier
    # run: the workflows ran and the confirmations were given before those PRs
    # could have merged. Mark them done so a re-run picks up where it left off
    # rather than re-prompting for sign-off on work that already shipped.
    for index, step in enumerate(steps):
        if is_assumed_completed(steps, index):
            _mark_skipped(step)

    return steps


def landed_prefix_end(plan: list[PlanStep]) -> int:
    """Index of the last ``l`` step whose PR had already landed, or -1 for none.

    Every step at or before this index is complete: the PR merged, and the
    workflow/confirm steps ahead of it must have run before it could. This is
    the boundary ``execute_plan`` resumes from and the display points at.

    Prefer ``is_assumed_completed`` for "is this step inside the prefix?" — the
    -1 makes that comparison work out on its own, but it slices from the *end*
    of a list if it is ever used as a bound directly.
    """
    return max(
        (i for i, s in enumerate(plan) if isinstance(s, LandStep) and s.already_landed),
        default=-1,
    )


def is_assumed_completed(plan: list[PlanStep], index: int) -> bool:
    """Whether the step at *index* sits inside the plan's already-landed prefix.

    Such a step was never run in this session; it is credited as done because
    the PRs after it could not have merged otherwise.
    """
    return index < landed_prefix_end(plan)


# ---------------------------------------------------------------------------
# Replanning: carry a previous run's progress over to a new plan
# ---------------------------------------------------------------------------

# A workflow or confirm step's identity for replanning: its kind, its text, and
# the PRs landed before it. A step means "once these PRs have landed, this
# holds", so a result recorded under the same PRs is still true in a new plan.
_CheckpointKey = tuple[str, str, frozenset[int]]


def _land_pr(step: LandStep, stack: list[StackEntry]) -> int | None:
    if step.pr_number is not None:
        return step.pr_number
    return None if step.already_landed else stack[step.entry_index].pr_number


def _checkpoint_keys(
    plan: list[PlanStep], stack: list[StackEntry]
) -> list[_CheckpointKey | None]:
    """Each step's ``_CheckpointKey``, or None for a land step."""
    landed: set[int] = set()
    keys: list[_CheckpointKey | None] = []
    for step in plan:
        if isinstance(step, LandStep):
            pr = _land_pr(step, stack)
            if pr is not None:
                landed.add(pr)
            keys.append(None)
        elif isinstance(step, WorkflowStep):
            keys.append(("w", step.workflow, frozenset(landed)))
        else:
            keys.append(("c", step.condition.strip(), frozenset(landed)))
    return keys


def _checkpoint_done(step: PlanStep) -> bool:
    if isinstance(step, WorkflowStep):
        return step.state in ("succeeded", "skipped")
    return isinstance(step, ConfirmStep) and step.confirmed


def _credit(new: PlanStep, old: PlanStep) -> None:
    """Record on *new* the result *old* reached in the previous run."""
    if isinstance(new, WorkflowStep) and isinstance(old, WorkflowStep):
        new.state = old.state
        new.error_message = ""
    elif isinstance(new, ConfirmStep):
        new.confirmed = True


def carry_over_progress(
    old: LandingContext, plan: list[PlanStep], stack: list[StackEntry]
) -> list[int]:
    """Credit *plan*'s workflow and confirm steps with results from *old*.

    *plan* is a freshly parsed plan for the current *stack*. Land steps need no
    help: whether a PR merged is GitHub's to say, and parsing already asked.
    A workflow or confirm step is credited when *old* completed a step with the
    same kind, text, and set of PRs landed before it — so adding, removing, or
    reordering steps keeps credit, while changing a step's text, or landing a
    different set of PRs ahead of it, makes it run again.

    Returns the indices (into ``old.plan``) of completed checkpoints that found
    no match in *plan*, so the caller can show what does not carry over.
    """
    unmatched: dict[_CheckpointKey, list[int]] = {}
    for index, key in enumerate(_checkpoint_keys(old.plan, old.stack)):
        if key is not None and _checkpoint_done(old.plan[index]):
            unmatched.setdefault(key, []).append(index)

    for step, key in zip(plan, _checkpoint_keys(plan, stack)):
        if key is None or not unmatched.get(key):
            continue
        previous = old.plan[unmatched[key].pop(0)]
        # A step parsing already credited (inside the landed prefix) keeps that
        # credit, but still consumes its match so it isn't reported as lost.
        if not _checkpoint_done(step):
            _credit(step, previous)

    return sorted(i for indices in unmatched.values() for i in indices)


def _resolve_land_step(
    ref: str,
    line_num: int,
    *,
    stack: list[StackEntry],
    next_index: int,
    pr_is_merged: Callable[[int], bool],
) -> LandStep:
    """Resolve one ``l`` line against the stack. Raises ValueError if it can't."""
    if not ref:
        # Bare 'l': take the next PR in the stack, as plans always have.
        if next_index >= len(stack):
            raise ValueError(
                f"Line {line_num}: too many 'l' steps — only {len(stack)} PRs in stack"
            )
        return LandStep(entry_index=next_index, pr_number=stack[next_index].pr_number)

    pr_number = _parse_pr_ref(ref, line_num)
    index = next(
        (i for i, e in enumerate(stack) if e.pr_number == pr_number),
        None,
    )

    if index is None:
        # Not in the stack: the only benign explanation is that it already
        # landed and was rebased away. Confirm that with GitHub rather than
        # silently skipping a typo'd or unrelated PR number.
        if not pr_is_merged(pr_number):
            raise ValueError(
                f"Line {line_num}: PR #{pr_number} is not in the stack and has "
                "not been merged — check the PR number, or that you are landing "
                "the stack this plan was written for"
            )
        if next_index:
            # Already-landed steps are the completed prefix of a plan, so one
            # appearing after a step we still have to land means the stack
            # merged out of the order the plan describes.
            raise ValueError(
                f"Line {line_num}: PR #{pr_number} has already merged, but the "
                f"plan lands it after PR #{stack[next_index - 1].pr_number}, "
                "which is still open — the stack no longer matches this plan"
            )
        return LandStep(entry_index=-1, pr_number=pr_number)

    if index < next_index:
        # An earlier 'l' step already claimed this stack entry. Catching this
        # here also keeps `stack[next_index]` below in range: every remaining
        # case has next_index < index < len(stack).
        raise ValueError(
            f"Line {line_num}: PR #{pr_number} is already landed by an earlier "
            "'l' step — each PR can be landed only once"
        )

    if index != next_index:
        expected = stack[next_index].pr_number
        raise ValueError(
            f"Line {line_num}: plan lands PR #{pr_number} next, but the next PR "
            f"in the stack is #{expected} — the stack no longer matches this plan"
        )

    return LandStep(entry_index=index, pr_number=pr_number)


# The conventional suffix for a landing plan. Plans autoland writes carry it,
# and editors key off it (see editors/vscode) to highlight the plan syntax.
PLAN_SUFFIX = ".autoland-plan"


def plan_from_file(path: Path, stack: list[StackEntry]) -> list[PlanStep]:
    """Load a landing plan from a file.

    The file uses the exact same format as the interactive editor (see
    ``format_plan_for_editor``): ``l [pr]`` / ``w <workflow>`` /
    ``c [condition]`` steps, with ``#`` comments and blank lines ignored. It is
    parsed by the same ``parse_plan`` the editor uses, so a file saved from
    ``-i`` (or written by hand in that format) round-trips.
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
