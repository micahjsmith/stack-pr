"""Stack discovery, reusing stack-pr's own ``get_stack``."""

from __future__ import annotations

from stack_pr import cli
from stack_pr.autoland import gh
from stack_pr.autoland.model import PRState, StackEntry

# ---------------------------------------------------------------------------
# Stack discovery (reuses stack-pr's get_stack)
# ---------------------------------------------------------------------------


def discover_stack(common: cli.CommonArgs) -> list[StackEntry]:
    """Discover the stack via stack-pr's own parser, bottom-to-top order."""
    return _stack_entries(
        cli.get_stack(base=common.base, head=common.head, verbose=common.verbose)
    )


def _stack_entries(raw: list[cli.StackEntry]) -> list[StackEntry]:
    entries: list[StackEntry] = []
    for e in raw:
        if not e.has_pr():
            continue  # commit not submitted yet — skip
        pr_number = int(cli.last(e.pr))
        entries.append(StackEntry(pr_url=e.pr, pr_number=pr_number, branch=e.head))
    return entries


def enrich_stack(stack: list[StackEntry]) -> None:
    """Fetch PR titles, review status, and current state from GitHub."""
    for entry in stack:
        try:
            data = gh.github.summary(entry.pr_number)
            entry.title = data.get("title", "")
            entry.review_decision = data.get("reviewDecision", "")
            if data.get("state") == "MERGED":
                entry.state = PRState.MERGED
        except RuntimeError:
            entry.title = "(could not fetch)"
