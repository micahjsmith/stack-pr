import sys
from pathlib import Path

import pytest

sys.path.append(str(Path(__file__).parent.parent / "src"))

from subprocess import SubprocessError

from stack_pr.cli import (
    edit_pr_base,
    force_push_with_lease,
    merge_queue_declined_branches,
    push_branches,
    reset_remote_base_branches,
    stale_lease_branches,
)

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


def test_edit_pr_base_success(mocker) -> None:  # noqa: ANN001
    run = mocker.patch(
        "stack_pr.cli.run_shell_command",
        return_value=mocker.Mock(returncode=0, stderr=b""),
    )

    edit_pr_base(PR, "main", verbose=False)

    run.assert_called_once()
    assert run.call_args.args[0] == ["gh", "pr", "edit", PR, "-B", "main"]


def test_edit_pr_base_merge_queue_skips_without_retry(mocker) -> None:  # noqa: ANN001
    # No extra_args -> nothing left to apply, so we just warn and move on.
    run = mocker.patch(
        "stack_pr.cli.run_shell_command",
        return_value=mocker.Mock(returncode=1, stderr=MERGE_QUEUE_ERR),
    )
    warn = mocker.patch("stack_pr.cli.warning")

    edit_pr_base(PR, "main", verbose=False)

    run.assert_called_once()
    warn.assert_called_once()


def test_edit_pr_base_merge_queue_retries_without_base(mocker) -> None:  # noqa: ANN001
    # First call (with -B) hits the merge queue; the retry drops -B so the
    # title/body edits still apply.
    run = mocker.patch(
        "stack_pr.cli.run_shell_command",
        side_effect=[
            mocker.Mock(returncode=1, stderr=MERGE_QUEUE_ERR),
            mocker.Mock(returncode=0, stderr=b""),
        ],
    )
    mocker.patch("stack_pr.cli.warning")

    edit_pr_base(
        PR,
        "main",
        extra_args=["-t", "title", "-F", "-"],
        verbose=False,
        input=b"body",
    )

    assert run.call_count == 2
    first, second = run.call_args_list
    assert first.args[0] == [
        "gh",
        "pr",
        "edit",
        PR,
        "-B",
        "main",
        "-t",
        "title",
        "-F",
        "-",
    ]
    # Retry omits "-B" / "main" but keeps the other edits and the piped body.
    assert second.args[0] == ["gh", "pr", "edit", PR, "-t", "title", "-F", "-"]
    assert second.kwargs["input"] == b"body"


def test_edit_pr_base_other_error_raises(mocker) -> None:  # noqa: ANN001
    mocker.patch(
        "stack_pr.cli.run_shell_command",
        return_value=mocker.Mock(returncode=1, stderr=b"some other failure"),
    )

    with pytest.raises(SubprocessError):
        edit_pr_base(PR, "main", verbose=False)


def test_reset_remote_base_branches_preserves_draft_status(mocker) -> None:  # noqa: ANN001
    # Resubmitting an existing stack must reset base branches but never toggle
    # the draft/ready status of the PRs (which is owned by the user).
    entries = []
    for i in range(2):
        e = mocker.Mock()
        e.has_pr.return_value = True
        e.pr = f"https://github.com/o/r/pull/{i}"
        entries.append(e)

    edit = mocker.patch("stack_pr.cli.edit_pr_base")
    run = mocker.patch("stack_pr.cli.run_shell_command")

    reset_remote_base_branches(entries, target="main", verbose=False)

    # Base branch is reset for every existing PR...
    assert edit.call_count == 2
    assert [c.args[0] for c in edit.call_args_list] == [e.pr for e in entries]
    # ...but no `gh pr ready`/`--undo` (or any other shell command) is issued.
    run.assert_not_called()


# --- force-with-lease push ------------------------------------------------


def test_stale_lease_branches_parses_git_stderr() -> None:
    stderr = (
        "To github.com:o/r.git\n"
        " ! [rejected]        micah/stack/2 -> micah/stack/2 (stale info)\n"
        " ! [rejected]        micah/stack/3 -> micah/stack/3 (stale info)\n"
        "error: failed to push some refs\n"
    )
    assert stale_lease_branches(stderr) == ["micah/stack/2", "micah/stack/3"]


def test_force_push_with_lease_uses_lease_flags(mocker) -> None:  # noqa: ANN001
    run = mocker.patch(
        "stack_pr.cli.run_shell_command",
        return_value=mocker.Mock(returncode=0, stderr=b""),
    )

    force_push_with_lease(["a:a", "b:b"], "origin", "main", verbose=False)

    run.assert_called_once()
    assert run.call_args.args[0] == [
        "git",
        "push",
        "--force-with-lease",
        "--atomic",
        "origin",
        "a:a",
        "b:b",
    ]


def test_force_push_with_lease_aborts_on_stale(mocker) -> None:  # noqa: ANN001
    stderr = b" ! [rejected]        s/2 -> s/2 (stale info)\nerror: failed to push\n"
    mocker.patch(
        "stack_pr.cli.run_shell_command",
        return_value=mocker.Mock(returncode=1, stderr=stderr),
    )
    err = mocker.patch("stack_pr.cli.error")

    with pytest.raises(SystemExit):
        force_push_with_lease(["s/2:s/2"], "origin", "main", verbose=False)

    # The abort message names the diverged branch.
    assert "s/2" in err.call_args.args[0]


def test_force_push_with_lease_reraises_other_errors(mocker) -> None:  # noqa: ANN001
    mocker.patch(
        "stack_pr.cli.run_shell_command",
        return_value=mocker.Mock(returncode=1, stderr=b"fatal: unrelated failure"),
    )

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


def test_push_branches_skips_queued_branch_and_pushes_the_rest(mocker) -> None:  # noqa: ANN001
    # The bottom PR of the stack is in a merge queue. GitHub declines any
    # update to its branch, and --atomic turns that into a rejection of all
    # ten branches -- so submit drops it and pushes the rest of the stack.
    st = []
    for i in (1, 2, 3):
        e = mocker.Mock()
        e.head = f"s/{i}"
        e.pr = f"https://github.com/o/r/pull/{40 + i}"
        e.has_pr.return_value = True
        st.append(e)
    run = mocker.patch(
        "stack_pr.cli.run_shell_command",
        side_effect=[
            mocker.Mock(returncode=1, stderr=queued_push_err("s/1")),
            mocker.Mock(returncode=0, stderr=b""),
        ],
    )
    warn = mocker.patch("stack_pr.cli.warning")

    push_branches(st, remote="origin", target="main", verbose=False)

    assert run.call_args_list[1].args[0] == [
        "git",
        "push",
        "--force-with-lease",
        "--atomic",
        "origin",
        "s/2:s/2",
        "s/3:s/3",
    ]
    # The user has to know which branch was left behind, and at which PR.
    assert "s/1 (#41)" in warn.call_args.args[0]


def test_push_branches_succeeds_when_only_branch_is_queued(mocker) -> None:  # noqa: ANN001
    e = mocker.Mock()
    e.head = "s/1"
    e.pr = "https://github.com/o/r/pull/41"
    e.has_pr.return_value = True
    run = mocker.patch(
        "stack_pr.cli.run_shell_command",
        return_value=mocker.Mock(returncode=1, stderr=queued_push_err("s/1")),
    )
    mocker.patch("stack_pr.cli.warning")

    push_branches([e], remote="origin", target="main", verbose=False)

    # Nothing left to push, so no second attempt.
    run.assert_called_once()


def test_force_push_with_lease_aborts_on_queued_branch_by_default(mocker) -> None:  # noqa: ANN001
    # Landing rebases a branch and pushes it: skipping the push would leave the
    # caller believing the remote has the rebased commits, so it must abort.
    mocker.patch(
        "stack_pr.cli.run_shell_command",
        return_value=mocker.Mock(returncode=1, stderr=queued_push_err("s/1")),
    )
    err = mocker.patch("stack_pr.cli.error")

    with pytest.raises(SystemExit):
        force_push_with_lease(["s/1:s/1"], "origin", "main", verbose=False)

    assert "s/1" in err.call_args.args[0]


def test_force_push_with_lease_raises_on_other_gh006(mocker) -> None:  # noqa: ANN001
    stderr = (
        b"remote: error: GH006: Protected branch update failed for refs/heads/s/1.\n"
        b"remote: - Changes must be made through a pull request.\n"
    )
    mocker.patch(
        "stack_pr.cli.run_shell_command",
        return_value=mocker.Mock(returncode=1, stderr=stderr),
    )

    with pytest.raises(SubprocessError):
        force_push_with_lease(["s/1:s/1"], "origin", "main", verbose=False)
