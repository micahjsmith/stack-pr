from subprocess import SubprocessError

import pytest

from stack_pr.cli import (
    edit_pr_base,
    force_push_with_lease,
    merge_queue_declined_branches,
    push_branches,
    reset_remote_base_branches,
    stale_lease_branches,
)
from tests.helpers import FakeShell, mock_entry

PR = "https://github.com/o/r/pull/42"

MERGE_QUEUE_ERR = (
    b"GraphQL: Cannot change the base branch because the branch has been "
    b"added to a merge queue. (updatePullRequest)"
)


# git's stderr when the remote declines a push to a branch queued for merging.
def queued_push_err(branch: str) -> bytes:
    return (
        f"remote: error: GH006: Protected branch update failed for "
        f"refs/heads/{branch}.        \n"
        "remote: \n"
        "remote: - A pull request for this branch has been added to a merge "
        "queue. Branches that        \n"
        "remote:   are queued for merging cannot be updated. To modify this "
        "branch, dequeue the        \n"
        "remote:   associated pull request.        \n"
        f" ! [remote rejected]       {branch} -> {branch} (protected branch "
        "hook declined)\n"
        " ! [remote rejected]       other -> other (atomic transaction failed)\n"
    ).encode()


def _push(*refspecs: str) -> list[str]:
    return ["git", "push", "--force-with-lease", "--atomic", "origin", *refspecs]


def test_edit_pr_base_success(fake_shell: FakeShell) -> None:
    edit_pr_base(PR, "main", verbose=False)

    assert fake_shell.commands == [["gh", "pr", "edit", PR, "-B", "main"]]


def test_edit_pr_base_merge_queue_skips_without_retry(
    fake_shell: FakeShell, capsys: pytest.CaptureFixture[str]
) -> None:
    # No extra_args -> nothing left to apply, so we just warn and move on.
    fake_shell.script((1, MERGE_QUEUE_ERR))

    edit_pr_base(PR, "main", verbose=False)

    assert fake_shell.commands == [["gh", "pr", "edit", PR, "-B", "main"]]
    out = capsys.readouterr().out
    assert "WARNING" in out
    assert f"Could not change the base branch of {PR}" in out


def test_edit_pr_base_merge_queue_retries_without_base(fake_shell: FakeShell) -> None:
    # First call (with -B) hits the merge queue; the retry drops -B so the
    # title/body edits still apply.
    fake_shell.script((1, MERGE_QUEUE_ERR))

    edit_pr_base(
        PR,
        "main",
        extra_args=["-t", "title", "-F", "-"],
        verbose=False,
        input=b"body",
    )

    assert fake_shell.commands == [
        ["gh", "pr", "edit", PR, "-B", "main", "-t", "title", "-F", "-"],
        # Retry omits "-B" / "main" but keeps the other edits and the piped body.
        ["gh", "pr", "edit", PR, "-t", "title", "-F", "-"],
    ]
    assert fake_shell.calls[1][1]["input"] == b"body"


def test_edit_pr_base_other_error_raises(fake_shell: FakeShell) -> None:
    fake_shell.script((1, b"some other failure"))

    with pytest.raises(SubprocessError):
        edit_pr_base(PR, "main", verbose=False)


def test_reset_remote_base_branches_preserves_draft_status(
    fake_shell: FakeShell,
) -> None:
    # Resubmitting an existing stack must reset base branches but never toggle
    # the draft/ready status of the PRs (which is owned by the user).
    entries = [mock_entry(0), mock_entry(1)]

    reset_remote_base_branches(entries, target="main", verbose=False)

    # The base branch is reset for every existing PR, and no `gh pr ready`
    # (or any other command) is issued.
    assert fake_shell.commands == [
        ["gh", "pr", "edit", e.pr, "-B", "main"] for e in entries
    ]


# --- force-with-lease push ------------------------------------------------


def test_stale_lease_branches_parses_git_stderr() -> None:
    stderr = (
        "To github.com:o/r.git\n"
        " ! [rejected]        micah/stack/2 -> micah/stack/2 (stale info)\n"
        " ! [rejected]        micah/stack/3 -> micah/stack/3 (stale info)\n"
        "error: failed to push some refs\n"
    )
    assert stale_lease_branches(stderr) == ["micah/stack/2", "micah/stack/3"]


def test_force_push_with_lease_uses_lease_flags(fake_shell: FakeShell) -> None:
    force_push_with_lease(["a:a", "b:b"], "origin", "main", verbose=False)

    assert fake_shell.commands == [_push("a:a", "b:b")]


def test_force_push_with_lease_aborts_on_stale(
    fake_shell: FakeShell, capsys: pytest.CaptureFixture[str]
) -> None:
    fake_shell.script(
        (1, b" ! [rejected]        s/2 -> s/2 (stale info)\nerror: failed to push\n")
    )

    with pytest.raises(SystemExit):
        force_push_with_lease(["s/2:s/2"], "origin", "main", verbose=False)

    # The abort message names the diverged branch.
    out = capsys.readouterr().out
    assert "ERROR" in out
    assert "s/2" in out


def test_force_push_with_lease_reraises_other_errors(fake_shell: FakeShell) -> None:
    fake_shell.script((1, b"fatal: unrelated failure"))

    with pytest.raises(SubprocessError):
        force_push_with_lease(["a:a"], "origin", "main", verbose=False)


def test_merge_queue_declined_branches_parses_gh006() -> None:
    stderr = queued_push_err("micah/stack/1").decode()
    assert merge_queue_declined_branches(stderr) == ["micah/stack/1"]


def test_merge_queue_declined_branches_ignores_other_protections() -> None:
    # Only a merge queue makes a branch un-pushable in a way we can work
    # around; other protected-branch refusals must stay hard errors.
    stderr = (
        "remote: error: GH006: Protected branch update failed for refs/heads/s/1.\n"
        'remote: - Required status check "ci" is expected.\n'
    )
    assert merge_queue_declined_branches(stderr) == []


def test_push_branches_skips_queued_branch_and_pushes_the_rest(
    fake_shell: FakeShell, capsys: pytest.CaptureFixture[str]
) -> None:
    # The bottom PR of the stack is in a merge queue. GitHub declines any
    # update to its branch, and --atomic turns that into a rejection of all
    # ten branches -- so submit drops it and pushes the rest of the stack.
    st = [mock_entry(40 + i, head=f"s/{i}") for i in (1, 2, 3)]
    fake_shell.script((1, queued_push_err("s/1")))

    push_branches(st, remote="origin", target="main", verbose=False)

    assert fake_shell.commands == [
        _push("s/1:s/1", "s/2:s/2", "s/3:s/3"),
        _push("s/2:s/2", "s/3:s/3"),
    ]
    # The user has to know which branch was left behind, and at which PR.
    assert "s/1 (#41)" in capsys.readouterr().out


def test_push_branches_succeeds_when_only_branch_is_queued(
    fake_shell: FakeShell,
) -> None:
    fake_shell.script((1, queued_push_err("s/1")))

    push_branches([mock_entry(41, head="s/1")], "origin", "main", verbose=False)

    # Nothing left to push, so no second attempt.
    assert fake_shell.commands == [_push("s/1:s/1")]


def test_force_push_with_lease_aborts_on_queued_branch_by_default(
    fake_shell: FakeShell, capsys: pytest.CaptureFixture[str]
) -> None:
    # Landing rebases a branch and pushes it: skipping the push would leave the
    # caller believing the remote has the rebased commits, so it must abort.
    fake_shell.script((1, queued_push_err("s/1")))

    with pytest.raises(SystemExit):
        force_push_with_lease(["s/1:s/1"], "origin", "main", verbose=False)

    out = capsys.readouterr().out
    assert "ERROR" in out
    assert "s/1" in out


def test_force_push_with_lease_raises_on_other_gh006(fake_shell: FakeShell) -> None:
    stderr = (
        b"remote: error: GH006: Protected branch update failed for refs/heads/s/1.\n"
        b"remote: - Changes must be made through a pull request.\n"
    )
    fake_shell.script((1, stderr))

    with pytest.raises(SubprocessError):
        force_push_with_lease(["s/1:s/1"], "origin", "main", verbose=False)
