import sys
from pathlib import Path

sys.path.append(str(Path(__file__).parent.parent / "src"))

from stack_pr import cli
from stack_pr.cli import (
    build_stack_pr_list,
    extract_toc_pr_ids,
    generate_toc,
)


def _entry(mocker, pr_num: int):  # noqa: ANN001, ANN202
    e = mocker.Mock()
    e.pr = f"https://github.com/o/r/pull/{pr_num}"
    return e


def test_extract_toc_pr_ids_bottom_first() -> None:
    body = "Stacked PRs:\n * __->__#3\n * #2\n * #1\n\n--- --- ---\nbody\n"
    assert extract_toc_pr_ids(body) == ["1", "2", "3"]


def test_extract_toc_pr_ids_requires_table() -> None:
    # No table at all.
    assert extract_toc_pr_ids("just a body mentioning #5") == []
    # Delimiter present but no header -> entries after the delimiter are ignored.
    assert extract_toc_pr_ids("text\n--- --- ---\n * #5\n") == []


def test_generate_toc_suppressed_for_single() -> None:
    assert generate_toc(["1"], "1") == ""
    assert generate_toc([], "1") == ""


def test_generate_toc_renders_top_first_with_arrow() -> None:
    toc = generate_toc(["1", "2", "3"], "2")
    assert toc == "Stacked PRs:\n * #3\n * __->__#2\n * #1\n\n"


def test_build_stack_pr_list_keeps_merged(mocker) -> None:  # noqa: ANN001
    # Active stack is #2, #3 (bottom-first); #1 has landed.
    st = [_entry(mocker, 2), _entry(mocker, 3)]
    body = (
        "Stacked PRs:\n"
        " * #3\n"
        " * #2\n"
        " * #1\n"
        "\n"
        "--- --- ---\n"
        "body text #999\n"  # prose ref after the delimiter must be ignored
    )
    mocker.patch("stack_pr.cli.get_pr_body", return_value=body)
    mocker.patch(
        "stack_pr.cli.get_pr_state",
        side_effect=lambda pid: {"1": "MERGED"}.get(pid, "OPEN"),
    )

    assert build_stack_pr_list(st) == ["1", "2", "3"]


def test_build_stack_pr_list_drops_open_absent(mocker) -> None:  # noqa: ANN001
    # Only #3 is active. #1 merged (keep), #9 left the stack but is still open
    # (drop).
    st = [_entry(mocker, 3)]
    body = "Stacked PRs:\n * #3\n * #9\n * #1\n\n--- --- ---\nx\n"
    mocker.patch("stack_pr.cli.get_pr_body", return_value=body)
    mocker.patch(
        "stack_pr.cli.get_pr_state",
        side_effect=lambda pid: {"1": "MERGED", "9": "OPEN"}.get(pid, "OPEN"),
    )

    assert build_stack_pr_list(st) == ["1", "3"]


def test_build_stack_pr_list_no_history(mocker) -> None:  # noqa: ANN001
    # Fresh stack: PR bodies have no cross-links table yet.
    st = [_entry(mocker, 1), _entry(mocker, 2)]
    mocker.patch("stack_pr.cli.get_pr_body", return_value="just the commit body")
    state = mocker.patch("stack_pr.cli.get_pr_state")

    assert build_stack_pr_list(st) == ["1", "2"]
    state.assert_not_called()


def test_build_stack_pr_list_keeps_closed(mocker) -> None:  # noqa: ANN001
    st = [_entry(mocker, 2)]
    body = "Stacked PRs:\n * #2\n * #1\n\n--- --- ---\nx\n"
    mocker.patch("stack_pr.cli.get_pr_body", return_value=body)
    mocker.patch(
        "stack_pr.cli.get_pr_state",
        side_effect=lambda pid: {"1": "CLOSED"}.get(pid, "OPEN"),
    )

    assert build_stack_pr_list(st) == ["1", "2"]


def test_generate_toc_single_active_with_history(mocker) -> None:  # noqa: ANN001
    # One active PR but merged history -> table still rendered.
    st = [_entry(mocker, 2)]
    body = "Stacked PRs:\n * #2\n * #1\n\n--- --- ---\nx\n"
    mocker.patch("stack_pr.cli.get_pr_body", return_value=body)
    mocker.patch(
        "stack_pr.cli.get_pr_state",
        side_effect=lambda pid: {"1": "MERGED"}.get(pid, "OPEN"),
    )

    pr_ids = cli.build_stack_pr_list(st)
    assert generate_toc(pr_ids, "2") == "Stacked PRs:\n * __->__#2\n * #1\n\n"
