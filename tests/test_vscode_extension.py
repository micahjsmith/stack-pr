import json
import re
from pathlib import Path

from stack_pr.autoland import gh
from stack_pr.autoland.model import ConfirmStep, LandStep, StackEntry, WorkflowStep
from stack_pr.autoland.plan import (
    PLAN_COMMENT_COLUMN,
    PLAN_COMMENT_GAP,
    format_plan_for_editor,
    parse_plan,
)

EXTENSION = Path(__file__).parent.parent / "editors" / "vscode"
EXAMPLE = EXTENSION / "examples" / "example.autoland-plan"
GENERATED = EXTENSION / "examples" / "generated.autoland-plan"


def test_example_plan_is_accepted_by_the_parser(mocker) -> None:  # noqa: ANN001
    # The example ships as the reference for the highlighted syntax, so it has
    # to be a plan autoland would actually run. Its PR-URL step is checked
    # against the current repository, which would otherwise ask GitHub.
    mocker.patch.object(
        gh.github, "owner_repo", return_value=("micahjsmith", "stack-pr")
    )
    stack = [
        StackEntry(
            pr_url=f"https://github.com/o/r/pull/{n}", pr_number=n, branch=f"b{n}"
        )
        for n in (101, 102, 103, 104)
    ]
    plan = parse_plan(EXAMPLE.read_text(), stack, pr_is_merged=lambda _pr: False)
    assert [type(s).__name__ for s in plan] == [
        "LandStep",
        "WorkflowStep",
        "ConfirmStep",
        "LandStep",
        "LandStep",
        "ConfirmStep",
        "LandStep",
    ]


def test_grammar_is_wired_to_the_language() -> None:
    # A mismatch here silently disables highlighting, which is invisible until
    # someone opens a plan.
    package = json.loads((EXTENSION / "package.json").read_text())
    grammar = json.loads(
        (EXTENSION / "syntaxes" / "autoland-plan.tmLanguage.json").read_text()
    )
    contributed = package["contributes"]["grammars"][0]
    language = package["contributes"]["languages"][0]

    assert contributed["scopeName"] == grammar["scopeName"]
    assert contributed["language"] == language["id"]
    assert (EXTENSION / contributed["path"]).is_file()
    assert (EXTENSION / language["configuration"]).is_file()


def test_generated_plan_matches_the_formatter_fixture() -> None:
    # The other half of this check is in the extension's own test suite, which
    # asserts the fixture is already formatted. Together they pin the property
    # that a plan straight out of `-i` needs no formatting: generator output ==
    # fixture == formatter output.
    stack = [
        StackEntry(
            pr_url="https://github.com/o/r/pull/101",
            pr_number=101,
            branch="b1",
            title="Add /widgets API endpoint",
        ),
        StackEntry(
            pr_url="https://github.com/o/r/pull/1024",
            pr_number=1024,
            branch="b2",
            title="Wire up the widgets UI",
        ),
        StackEntry(
            pr_url="https://github.com/o/r/pull/103",
            pr_number=103,
            branch="b3",
            title="",
        ),
    ]
    plan = [
        LandStep(entry_index=0, pr_number=101),
        WorkflowStep(workflow="deploy.yaml"),
        ConfirmStep(condition="QA sign-off complete"),
        LandStep(entry_index=1, pr_number=1024),
        LandStep(entry_index=2, pr_number=103),
    ]
    assert format_plan_for_editor(stack, plan) == GENERATED.read_text()


def test_comment_layout_agrees_with_the_formatter() -> None:
    # Two implementations of the same layout in two languages; if they disagree
    # on either number, formatting a generated plan would shift every comment.
    source = (EXTENSION / "src" / "format.js").read_text()
    constants = dict(re.findall(r"const (COMMENT_\w+) = (\d+);", source))
    assert constants == {
        "COMMENT_COLUMN": str(PLAN_COMMENT_COLUMN),
        "COMMENT_GAP": str(PLAN_COMMENT_GAP),
    }
