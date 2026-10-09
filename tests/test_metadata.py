"""Adding and stripping the stack-info trailer on a stack's commits."""

from __future__ import annotations

from pathlib import Path

import pytest

from stack_pr.cli import (
    StackEntry,
    add_or_update_metadata,
    get_stack,
    init_local_branches,
    remove_stack_info,
    set_base_branches,
    strip_metadata,
)
from tests.helpers import (
    PR_URL,
    branches,
    commit_message,
    git,
    init_repo,
    init_stack_repo,
    stack_info,
)

TEMPLATE = "$USERNAME/stack/$ID"

pytestmark = pytest.mark.usefixtures("gh_username")


def _local_stack(repo: Path) -> list[StackEntry]:
    """The stack on top of main, with branches and bases as submit sets them."""
    st = get_stack("main", "HEAD", verbose=False)
    init_local_branches(st, "origin", verbose=False, branch_name_template=TEMPLATE)
    set_base_branches(st, "main")
    return st


def _commit(repo: Path, msg: str) -> None:
    (repo / "change.txt").write_text("change\n")
    git(repo, "add", "change.txt")
    git(repo, "commit", "-q", "-m", msg)


# --- init_local_branches ----------------------------------------------------


def test_init_local_branches_names_new_branches_after_the_remote_ones(
    tmp_path: Path,
    monkeypatch,  # noqa: ANN001
) -> None:
    local, _remote = init_stack_repo(tmp_path, 2, submitted=False)
    # Someone else's stack already holds ids 1-3 on the remote.
    for i in (1, 3):
        git(local, "push", "-q", "origin", f"main:refs/heads/TestBot/stack/{i}")
    monkeypatch.chdir(local)

    st = get_stack("main", "HEAD", verbose=False)
    init_local_branches(st, "origin", verbose=False, branch_name_template=TEMPLATE)

    assert [e.head for e in st] == ["TestBot/stack/4", "TestBot/stack/5"]
    for e in st:
        assert git(local, "rev-parse", e.head).strip() == e.commit.commit_id()


def test_init_local_branches_keeps_existing_heads(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    local, _remote = init_stack_repo(tmp_path, 2, submitted=True)
    monkeypatch.chdir(local)

    st = get_stack("main", "HEAD", verbose=False)
    init_local_branches(st, "origin", verbose=False, branch_name_template=TEMPLATE)

    assert [e.head for e in st] == ["TestBot/stack/1", "TestBot/stack/2"]
    assert {"TestBot/stack/1", "TestBot/stack/2"} <= branches(local)


# --- add_or_update_metadata -------------------------------------------------


def test_add_metadata_appends_the_trailer_to_every_commit(
    tmp_path: Path,
    monkeypatch,  # noqa: ANN001
) -> None:
    local, _remote = init_stack_repo(tmp_path, 2, submitted=False)
    monkeypatch.chdir(local)
    st = _local_stack(local)

    needs_rebase = False
    for i, e in enumerate(st, start=1):
        e.pr = PR_URL.format(i)
        needs_rebase = add_or_update_metadata(
            e, needs_rebase=needs_rebase, verbose=False
        )

    for i, e in enumerate(st, start=1):
        assert commit_message(local, e.head) == (
            f"c{i}\n\nBody of c{i}.\n\n" + stack_info(i, e.head)
        )
    # The rewritten commits still form a stack.
    assert (
        git(local, "rev-parse", f"{st[1].head}^").strip()
        == git(local, "rev-parse", st[0].head).strip()
    )


@pytest.mark.parametrize(
    ("msg", "expected"),
    [
        # An existing trailer block gains the stack-info line at its end.
        (
            "c\n\nbody\n\nCo-Authored-By: A <a@b.c>",
            "c\n\nbody\n\nCo-Authored-By: A <a@b.c>\n{si}",
        ),
        # A title alone gets the trailer as a new paragraph.
        ("c", "c\n\n{si}"),
    ],
)
def test_add_metadata_joins_an_existing_trailer_block(
    tmp_path: Path,
    monkeypatch,  # noqa: ANN001
    msg: str,
    expected: str,
) -> None:
    local = init_repo(tmp_path / "repo")
    git(local, "checkout", "-q", "-b", "feature")
    _commit(local, msg)
    monkeypatch.chdir(local)
    (e,) = get_stack("main", "HEAD", verbose=False)
    e.head = "feature"
    e.pr = PR_URL.format(1)

    add_or_update_metadata(e, needs_rebase=False, verbose=False)

    assert commit_message(local, "feature") == expected.format(
        si=stack_info(1, "feature")
    )
    # Stripping it again restores the original message.
    (e,) = get_stack("main", "HEAD", verbose=False)
    e.head = "feature"
    sha = strip_metadata(e, needs_rebase=False, verbose=False)
    assert commit_message(local, sha) == msg


def test_add_metadata_leaves_a_commit_that_already_has_it(
    tmp_path: Path,
    monkeypatch,  # noqa: ANN001
) -> None:
    local, _remote = init_stack_repo(tmp_path, 1, submitted=True)
    monkeypatch.chdir(local)
    (e,) = _local_stack(local)
    before = git(local, "rev-parse", e.head)

    changed = add_or_update_metadata(e, needs_rebase=False, verbose=False)

    assert changed is False
    assert git(local, "rev-parse", e.head) == before


def test_add_metadata_requires_a_head_branch(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    local, _remote = init_stack_repo(tmp_path, 1, submitted=False)
    monkeypatch.chdir(local)
    (e,) = get_stack("main", "HEAD", verbose=False)

    with pytest.raises(RuntimeError):
        add_or_update_metadata(e, needs_rebase=False, verbose=False)


# --- strip_metadata ---------------------------------------------------------


def test_strip_metadata_removes_only_the_trailer(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    local, _remote = init_stack_repo(tmp_path, 1, submitted=True)
    monkeypatch.chdir(local)
    (e,) = _local_stack(local)

    sha = strip_metadata(e, needs_rebase=False, verbose=False)

    assert sha == git(local, "rev-parse", e.head).strip()
    assert commit_message(local, sha) == "c1\n\nBody of c1."
    # Only the message changed.
    assert git(local, "rev-parse", f"{sha}^{{tree}}") == git(
        local, "rev-parse", f"{e.commit.commit_id()}^{{tree}}"
    )


@pytest.mark.parametrize(
    "msg",
    [
        # No metadata at all.
        "title\n\nbody",
        # A title alone, no body.
        "title",
        # "stack-info:" in the title is not a trailer.
        "stack-info: PR: x, branch: y",
        # Several paragraphs are kept as they are.
        "title\n\npara one\n\npara two",
    ],
)
def test_strip_metadata_keeps_other_messages_intact(
    tmp_path: Path,
    monkeypatch,  # noqa: ANN001
    msg: str,
) -> None:
    local = init_repo(tmp_path / "repo")
    git(local, "checkout", "-q", "-b", "feature")
    _commit(local, msg)
    monkeypatch.chdir(local)
    (e,) = get_stack("main", "HEAD", verbose=False)
    e.head = "feature"

    sha = strip_metadata(e, needs_rebase=False, verbose=False)

    assert commit_message(local, sha) == msg


def test_strip_metadata_keeps_a_following_trailer_a_trailer(
    tmp_path: Path,
    monkeypatch,  # noqa: ANN001
) -> None:
    local = init_repo(tmp_path / "repo")
    git(local, "checkout", "-q", "-b", "feature")
    _commit(
        local,
        "title\n\nbody\n\n" + stack_info(1, "b") + "\nCo-Authored-By: A <a@b.c>",
    )
    monkeypatch.chdir(local)
    (e,) = get_stack("main", "HEAD", verbose=False)
    e.head = "feature"

    sha = strip_metadata(e, needs_rebase=False, verbose=False)

    assert commit_message(local, sha) == "title\n\nbody\n\nCo-Authored-By: A <a@b.c>"


SI = stack_info(1, "b")


@pytest.mark.parametrize(
    ("msg", "expected"),
    [
        # stack-info as the only trailer.
        (f"title\n\nbody\n\n{SI}", "title\n\nbody"),
        # No body, only a title.
        (f"title\n\n{SI}", "title"),
        # Before, after, and between other trailers.
        (f"title\n\nbody\n\n{SI}\nA: 1", "title\n\nbody\n\nA: 1"),
        (f"title\n\nbody\n\nA: 1\n{SI}", "title\n\nbody\n\nA: 1"),
        (f"title\n\nbody\n\nA: 1\n{SI}\nB: 2", "title\n\nbody\n\nA: 1\nB: 2"),
        # A paragraph of its own in the middle of the message.
        (f"title\n\nbody\n\n{SI}\n\nmore", "title\n\nbody\n\nmore"),
        # "stack-info:" in the title is not a trailer.
        (SI, SI),
    ],
)
def test_remove_stack_info(msg: str, expected: str) -> None:
    assert remove_stack_info(msg) == expected
