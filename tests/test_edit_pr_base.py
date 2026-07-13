import sys
from pathlib import Path

import pytest

sys.path.append(str(Path(__file__).parent.parent / "src"))

from subprocess import SubprocessError

from stack_pr.cli import (
    edit_pr_base,
    force_push_with_lease,
    reset_remote_base_branches,
    stale_lease_branches,
)

PR = "https://github.com/o/r/pull/42"

MERGE_QUEUE_ERR = (
    b"GraphQL: Cannot change the base branch because the branch has been "
    b"added to a merge queue. (updatePullRequest)"
)


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
