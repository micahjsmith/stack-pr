import json
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).parent.parent / "src"))

from stack_pr import autoland
from stack_pr.autoland import StackEntry, parse_plan

EXTENSION = Path(__file__).parent.parent / "editors" / "vscode"
EXAMPLE = EXTENSION / "examples" / "example.autoland-plan"


def test_example_plan_is_accepted_by_the_parser(mocker) -> None:  # noqa: ANN001
    # The example ships as the reference for the highlighted syntax, so it has
    # to be a plan autoland would actually run. Its PR-URL step is checked
    # against the current repository, which would otherwise ask GitHub.
    mocker.patch.object(
        autoland.github, "owner_repo", return_value=("micahjsmith", "stack-pr")
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
