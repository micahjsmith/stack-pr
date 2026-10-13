"""``l [<pr>]``: land a PR in the stack."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from stack_pr.autoland import gh
from stack_pr.autoland.pr_state import PRState
from stack_pr.autoland.steps.base import AutolandStep, PlanParser, register_step
from stack_pr.autoland.steps.execution import StepContext, StepResult
from stack_pr.autoland.steps.view import StepOutcome, StepTone, StepView

if TYPE_CHECKING:
    from stack_pr.autoland.model import StackEntry

# A pinned PR reference: a bare number, or the PR's URL. The owner and repo are
# captured so a URL can be checked against the repo being landed into. Note that
# the '#123' spelling is deliberately unsupported: '#' starts a comment, so
# 'l #123' is indistinguishable from a bare 'l' with a comment after it.
RE_PR_URL = re.compile(r"^https?://[^/]+/([^/]+)/([^/]+)/pull/(\d+)/?$")

_PR_STATE_STATUS = {
    PRState.PENDING: (StepOutcome.PENDING, "Pending", StepTone.MUTED),
    PRState.WAITING_FOR_APPROVAL: (
        StepOutcome.ACTIVE,
        "Waiting for approval",
        StepTone.AWAITING_REVIEW,
    ),
    PRState.WAITING_FOR_CHECKS: (
        StepOutcome.ACTIVE,
        "Waiting for checks",
        StepTone.AWAITING_CHECKS,
    ),
    PRState.IN_MERGE_QUEUE: (StepOutcome.ACTIVE, "In merge queue", StepTone.IN_QUEUE),
    PRState.MERGING: (StepOutcome.ACTIVE, "Merging", StepTone.MERGING),
    PRState.WAITING_FOR_WORKFLOW: (
        StepOutcome.ACTIVE,
        "Waiting for workflow",
        StepTone.AWAITING_WORKFLOW,
    ),
    PRState.MERGED: (StepOutcome.DONE, "Merged", StepTone.SUCCESS),
    PRState.FAILED: (StepOutcome.FAILED, "Failed", StepTone.FAILURE),
}


@register_step
@dataclass
class LandStep(AutolandStep):
    """Land a PR in the stack, through the merge queue or directly.

    ``entry_index`` indexes into ``LandingContext.stack``, or is ``-1`` for a
    PR that has already landed: such a PR is no longer part of the stack, so
    the step is skipped at execution time.

    ``pr_number`` is set when the plan pinned a specific PR (``l 123``) and is
    ``None`` for a bare, positional ``l``. It is the only thing identifying an
    already-landed step, so it is always set when ``entry_index`` is ``-1``.
    """

    kind = "land"
    keyword = "l"
    label = "Land"

    entry_index: int
    pr_number: int | None = None

    @property
    def already_landed(self) -> bool:
        return self.entry_index < 0

    @classmethod
    def parse(cls, args: str, parser: PlanParser) -> LandStep:
        """Resolve one ``l`` line against the stack.

        An ``l`` step may pin the PR it lands (``l 123`` or ``l <pr url>``)
        instead of taking the next PR in the stack positionally. A pinned PR
        that has already landed is no longer in the stack; such a step
        resolves to ``entry_index=-1`` and is skipped at execution time, so one
        plan file stays valid as the stack lands piece by piece.
        """
        step = _resolve_land_step(args, parser)
        # An already-landed step claims no stack entry, so the next live 'l'
        # still expects the PR currently at the bottom of the stack.
        if not step.already_landed:
            parser.next_index += 1
        return step

    def plan_line(self, stack: list[StackEntry]) -> tuple[str, str]:
        if self.already_landed:
            return f"l {self.pr_number}", "already landed"
        entry = stack[self.entry_index]
        return f"l {entry.pr_number}", entry.title or ""

    @property
    def marks_landed_prefix(self) -> bool:
        return self.already_landed

    def landed_pr(self, stack: list[StackEntry]) -> int | None:
        if self.pr_number is not None:
            return self.pr_number
        return None if self.already_landed else stack[self.entry_index].pr_number

    def view(
        self, stack: list[StackEntry], *, is_next: bool, assumed_completed: bool
    ) -> StepView:
        if self.already_landed:
            # The PR is no longer in the stack, so there is no entry to read a
            # title or progress from; the pinned number is all there is.
            key = f"#{self.pr_number}"
            return StepView(
                outcome=StepOutcome.DONE,
                status="Landed",
                tone=StepTone.SUCCESS,
                key=key,
                description=f"Land PR {key} (already landed)",
                data={"pr_number": self.pr_number, "pr_url": None, "title": None},
            )
        entry = stack[self.entry_index]
        outcome, status, tone = _PR_STATE_STATUS.get(
            entry.state, (StepOutcome.PENDING, str(entry.state), StepTone.PLAIN)
        )
        detail = []
        if entry.error_message:
            detail.append(entry.error_message)
        # Retry counts only matter once something has been retried.
        if entry.check_retries or entry.queue_retries:
            detail.append(
                f"retries CI {entry.check_retries} / MQ {entry.queue_retries}"
            )
        key = f"#{entry.pr_number}"
        title = entry.title or "(untitled)"
        return StepView(
            outcome=outcome,
            status=status,
            tone=tone,
            key=key,
            note=title,
            description=f"Land PR {key}: {title}",
            detail=tuple(detail),
            data={
                "pr_number": entry.pr_number,
                "pr_url": entry.pr_url,
                "title": entry.title,
            },
        )

    def execute(self, sc: StepContext) -> StepResult:
        """Wait for approval and checks, merge (through the queue, or
        directly), then rebase and re-submit whatever is left of the stack
        above."""
        ui, repo = sc.services.ui, sc.services.repo
        if self.already_landed:
            ui.print(f"\n[green]PR #{self.pr_number} already landed, skipping[/green]")
            # Later steps follow the code of the prefix's last PR; looking up
            # the others would only cost a fetch each.
            if sc.ends_landed_prefix:
                return StepResult.done(
                    landed_sha=repo.landed_sha(sc.common, self.pr_number)
                )
            return StepResult.done()

        entry = sc.run.stack[self.entry_index]
        sc.run.current_index = self.entry_index
        if entry.is_merged():
            ui.print(f"\n[green]PR #{entry.pr_number} already merged, skipping[/green]")
            return StepResult.done(
                landed_sha=repo.landed_sha(sc.common, entry.pr_number)
            )

        ui.print(sc.banner(f"Landing PR #{entry.pr_number} — {entry.title}"))
        waits, opts = sc.services.waits, sc.opts
        # Each wait may find the PR merged by then (e.g. by hand), which ends
        # the landing early.
        if not waits.wait_for_approval(entry, opts=opts):
            return StepResult.abort(
                f"PR #{entry.pr_number} was closed while waiting for approval"
            )
        if not entry.is_merged() and not waits.wait_for_checks(entry, opts=opts):
            return StepResult.abort(
                f"PR #{entry.pr_number} checks failed after retries"
            )
        if not entry.is_merged() and not waits.merge_and_wait(entry, opts=opts):
            return StepResult.abort(f"PR #{entry.pr_number} failed to merge")
        landed_sha = repo.landed_sha(sc.common, entry.pr_number)

        # Rebase + resubmit whenever commits remain above the one we just
        # landed — not only when more *land steps* follow. On a partial land,
        # the PRs we're leaving open still need their bases rebased onto the
        # newly-landed commit.
        if self.entry_index < len(sc.run.stack) - 1:
            try:
                repo.rebase_and_resubmit(sc.common)
            except Exception as e:  # noqa: BLE001 - report any resubmit failure
                return StepResult.abort(
                    f"Rebase failed after merging #{entry.pr_number}: {e}",
                    landed_sha=landed_sha,
                )
        return StepResult.done(landed_sha=landed_sha)


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


def _resolve_land_step(ref: str, parser: PlanParser) -> LandStep:
    """Resolve one ``l`` line against the stack. Raises ValueError if it can't."""
    stack, next_index, line_num = parser.stack, parser.next_index, parser.line_num
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
        if not parser.pr_is_merged(pr_number):
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
