"""Creating PRs and verifying a stack against GitHub (cli.create_pr, cli.verify)."""

from __future__ import annotations

import pytest

from stack_pr.cli import CommitHeader, StackEntry, create_pr, verify
from tests.helpers import PR_URL, FakeGitHub


def _entry(
    msg: str = "Title\n\nBody.",
    *,
    pr: str | None = None,
    head: str | None = "TestBot/stack/1",
    base: str | None = "main",
) -> StackEntry:
    """A stack entry for a commit with message *msg*, as get_stack builds it."""
    raw = (
        f"{'a' * 40}\ntree {'b' * 40}\nauthor A <a@b.c> 0 +0000\n\n"
        + "\n".join(f"    {line}" for line in msg.splitlines())
        + "\n"
    )
    return StackEntry(CommitHeader(raw), _pr=pr, _head=head, _base=base)


# --- create_pr --------------------------------------------------------------


def test_create_pr_opens_a_pr_from_the_commit(fake_gh: FakeGitHub) -> None:
    e = _entry("Add a widget\n\nIt does things.", base="TestBot/stack/0")

    create_pr(e, is_draft=False)

    ((cmd, stdin),) = fake_gh.calls
    assert cmd == [
        "gh", "pr", "create",
        "-B", "TestBot/stack/0",
        "-H", "TestBot/stack/1",
        "-t", "Add a widget",
        "-F", "-",
    ]  # fmt: skip
    assert stdin == "Add a widget\n\nIt does things."
    # The entry now points at the PR gh printed.
    assert e.pr == PR_URL.format(1)


def test_create_pr_passes_draft_and_reviewer(fake_gh: FakeGitHub) -> None:
    create_pr(_entry(), is_draft=True, reviewer="octocat")

    (cmd,) = fake_gh.commands
    assert cmd[-3:] == ["--reviewer", "octocat", "--draft"]


def test_create_pr_skips_an_entry_that_has_a_pr(fake_gh: FakeGitHub) -> None:
    create_pr(_entry(pr=PR_URL.format(7)), is_draft=False)

    assert fake_gh.commands == []


def test_create_pr_requires_head_and_base(fake_gh: FakeGitHub) -> None:
    with pytest.raises(RuntimeError):
        create_pr(_entry(base=None), is_draft=False)
    assert fake_gh.commands == []


# --- verify -----------------------------------------------------------------


@pytest.fixture
def two_prs(fake_gh: FakeGitHub) -> list[StackEntry]:
    """Two entries whose PRs on GitHub match them exactly."""
    fake_gh.add_pr(1, head="TestBot/stack/1", base="main")
    fake_gh.add_pr(2, head="TestBot/stack/2", base="TestBot/stack/1")
    return [
        _entry(pr=PR_URL.format(1), head="TestBot/stack/1", base="main"),
        _entry(pr=PR_URL.format(2), head="TestBot/stack/2", base="TestBot/stack/1"),
    ]


def test_verify_accepts_a_stack_matching_github(two_prs: list[StackEntry]) -> None:
    verify(two_prs, check_base=True)


@pytest.mark.parametrize(
    ("pr", "fields", "reason"),
    [
        (1, {"state": "CLOSED"}, "not in 'OPEN' state"),
        (2, {"state": "MERGED"}, "not in 'OPEN' state"),
        (2, {"number": 3}, "PR number on github mismatches"),
        (2, {"headRefName": "someone/else"}, "Head branch name on github mismatches"),
    ],
)
def test_verify_rejects_a_pr_that_does_not_match(  # noqa: PLR0917
    fake_gh: FakeGitHub,
    two_prs: list[StackEntry],
    capsys,  # noqa: ANN001
    pr: int,
    fields: dict[str, object],
    reason: str,
) -> None:
    fake_gh.prs[pr].update(fields)

    with pytest.raises(RuntimeError):
        verify(two_prs)

    assert reason in capsys.readouterr().out


def test_verify_checks_bases_and_mergeability_only_when_asked(
    fake_gh: FakeGitHub,
    two_prs: list[StackEntry],
    capsys,  # noqa: ANN001
) -> None:
    # submit tolerates a diverged base, since it is about to fix it up; land
    # does not.
    fake_gh.prs[2]["baseRefName"] = "main"
    verify(two_prs)
    with pytest.raises(RuntimeError):
        verify(two_prs, check_base=True)
    assert "Base branch name on github mismatches" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("merge_state", "ok"),
    [("CLEAN", True), ("UNSTABLE", True), ("UNKNOWN", True), ("BLOCKED", False)],
)
def test_verify_requires_the_bottom_pr_to_be_mergeable(
    fake_gh: FakeGitHub,
    two_prs: list[StackEntry],
    merge_state: str,
    ok: bool,
) -> None:
    fake_gh.prs[1]["mergeStateStatus"] = merge_state
    # Only the bottom PR is about to merge; the rest may be blocked meanwhile.
    fake_gh.prs[2]["mergeStateStatus"] = "BLOCKED"

    if ok:
        verify(two_prs, check_base=True)
    else:
        with pytest.raises(RuntimeError):
            verify(two_prs, check_base=True)


@pytest.mark.parametrize(
    ("entry", "reason"),
    [
        (_entry(pr=None), "missing some information"),
        (_entry(pr="https://github.com/o/r/pull/abc"), "Bad PR link"),
    ],
)
def test_verify_rejects_bad_metadata_without_asking_github(
    fake_gh: FakeGitHub,
    capsys,  # noqa: ANN001
    entry: StackEntry,
    reason: str,
) -> None:
    with pytest.raises(RuntimeError):
        verify([entry])

    assert reason in capsys.readouterr().out
    assert fake_gh.commands == []
