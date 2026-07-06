import sys
from pathlib import Path

import pytest

sys.path.append(str(Path(__file__).parent.parent / "src"))

from subprocess import SubprocessError

from stack_pr.cli import edit_pr_base

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
    assert first.args[0] == ["gh", "pr", "edit", PR, "-B", "main", "-t", "title", "-F", "-"]
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
