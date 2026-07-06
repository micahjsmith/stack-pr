import argparse
import configparser
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).parent.parent / "src"))

import pytest

from stack_pr import autoland
from stack_pr.autoland import (
    AutolandCheckpointer,
    AutolandOptions,
    CheckStatus,
    ConfirmStep,
    LandingContext,
    LandStep,
    StackEntry,
    WorkflowStep,
    evaluate_checks,
    parse_plan,
)
from stack_pr.cli import CommonArgs


def _args(**overrides) -> argparse.Namespace:  # noqa: ANN003
    base = {
        "poll_interval": None,
        "max_check_retries": None,
        "max_queue_retries": None,
        "workflow_timeout": None,
        "dry_run": False,
        "branch": None,
        "interactive": False,
        "resume": False,
        "state_file": None,
        "always_cleanup": False,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def _common() -> CommonArgs:
    return CommonArgs(
        base="main",
        head="HEAD",
        remote="origin",
        target="main",
        hyperlinks=False,
        verbose=False,
        branch_name_template="$USERNAME/stack/$ID",
        show_tips=False,
        land_disabled=False,
    )


# --- options -------------------------------------------------------------


def test_options_precedence_flag_over_config_over_default() -> None:
    cfg = configparser.ConfigParser()
    cfg.add_section("autoland")
    cfg.set("autoland", "merge_queue", "true")
    cfg.set("autoland", "poll_interval", "99")
    cfg.set("autoland", "required_checks", "a, b ,c")

    opts = AutolandOptions.from_config_and_args(
        cfg, _args(max_check_retries=7)
    )

    assert opts.merge_queue is True
    assert opts.poll_interval == 99  # from config
    assert opts.max_check_retries == 7  # from flag
    assert opts.max_queue_retries == autoland.DEFAULT_MAX_QUEUE_RETRIES  # default
    assert opts.required_checks == ["a", "b", "c"]


# --- merge-queue gate ----------------------------------------------------


def test_run_autoland_requires_merge_queue() -> None:
    cfg = configparser.ConfigParser()  # no [autoland] -> merge_queue False
    with pytest.raises(NotImplementedError):
        autoland.run_autoland(_common(), _args(), cfg)


# --- check evaluation (pure: takes the check list) ------------------------


def test_evaluate_checks_all_passing_required() -> None:
    checks = [
        {"name": "ci", "bucket": "pass"},
        {"name": "lint", "bucket": "pass"},
        {"name": "other", "bucket": "fail"},  # not required -> ignored
    ]
    assert evaluate_checks(checks, ["ci", "lint"]).status == CheckStatus.ALL_PASSING


def test_evaluate_checks_failure_collects_run_id() -> None:
    checks = [
        {"name": "ci", "bucket": "pass"},
        {
            "name": "lint",
            "bucket": "fail",
            "link": "https://github.com/o/r/actions/runs/12345/job/9",
        },
    ]
    res = evaluate_checks(checks, ["ci", "lint"])
    assert res.status == CheckStatus.FAILED
    assert res.failed_names == ["lint"]
    assert res.failed_runs == [12345]


def test_evaluate_checks_missing_required_is_not_started() -> None:
    res = evaluate_checks([{"name": "ci", "bucket": "pass"}], ["ci", "lint"])
    assert res.status == CheckStatus.NOT_STARTED


def test_evaluate_checks_empty_required_gates_on_all() -> None:
    checks = [
        {"name": "ci", "bucket": "pass"},
        {"name": "deploy", "bucket": "skipping"},  # ignored
        {"name": "lint", "bucket": "pending"},
    ]
    assert evaluate_checks(checks, []).status == CheckStatus.PENDING


def test_evaluate_checks_empty_required_no_checks() -> None:
    assert evaluate_checks([], []).status == CheckStatus.NOT_STARTED


# --- merge status polling (GitHub.poll_merge) ----------------------------


def test_poll_merge_merged(mocker) -> None:  # noqa: ANN001
    mocker.patch.object(autoland.github, "pr_state", return_value="MERGED")
    assert autoland.github.poll_merge(1).merged is True


def test_poll_merge_closed(mocker) -> None:  # noqa: ANN001
    mocker.patch.object(autoland.github, "pr_state", return_value="CLOSED")
    assert autoland.github.poll_merge(1).error == "PR was closed"


def test_poll_merge_booted(mocker) -> None:  # noqa: ANN001
    mocker.patch.object(autoland.github, "pr_state", return_value="OPEN")
    mocker.patch.object(autoland.github, "in_merge_queue", return_value=False)
    assert autoland.github.poll_merge(1).booted is True


def test_poll_merge_still_queued(mocker) -> None:  # noqa: ANN001
    mocker.patch.object(autoland.github, "pr_state", return_value="OPEN")
    mocker.patch.object(autoland.github, "in_merge_queue", return_value=True)
    res = autoland.github.poll_merge(1)
    assert not res.merged
    assert not res.booted
    assert not res.error


# --- plan parsing --------------------------------------------------------


def _stack(n: int) -> list:
    return [
        StackEntry(pr_url=f"u/{i}", pr_number=i, branch=f"b{i}") for i in range(n)
    ]


def test_parse_plan_with_workflow_and_confirm() -> None:
    text = "l\nw deploy.yaml\nc ship it\nl\n"
    steps = parse_plan(text, _stack(2))
    assert [type(s) for s in steps] == [LandStep, WorkflowStep, ConfirmStep, LandStep]
    assert steps[1].workflow == "deploy.yaml"
    assert steps[2].message == "ship it"
    assert [s.entry_index for s in steps if isinstance(s, LandStep)] == [0, 1]


def test_parse_plan_rejects_old_deploy_letter() -> None:
    # The 'd' letter was renamed to 'w'; it should no longer be recognized.
    with pytest.raises(ValueError, match="unrecognized step"):
        parse_plan("l\nd deploy.yaml\n", _stack(1))


def test_parse_plan_requires_all_lands() -> None:
    with pytest.raises(ValueError, match="all PRs must be landed"):
        parse_plan("l\n", _stack(2))


def test_parse_plan_rejects_unknown_step() -> None:
    with pytest.raises(ValueError, match="unrecognized step"):
        parse_plan("frobnicate\n", _stack(1))


# --- state round-trip ----------------------------------------------------


def test_state_round_trip(tmp_path) -> None:  # noqa: ANN001
    ctx = LandingContext(
        stack=_stack(2), plan=parse_plan("l\nw deploy.yaml\nl\n", _stack(2))
    )
    ctx.current_step = 1
    ctx.last_landed_sha = "abc"
    ctx.stack[0].state = autoland.PRState.MERGED  # exercise enum round-trip

    sf = tmp_path / "state.json"
    AutolandCheckpointer(path=sf, branch="feat", base="main").save(ctx)

    cp, loaded = AutolandCheckpointer.load(sf)
    assert cp.branch == "feat"
    assert cp.base == "main"
    assert loaded.current_step == 1
    assert loaded.last_landed_sha == "abc"
    assert loaded.stack[0].state == autoland.PRState.MERGED
    assert [e.pr_number for e in loaded.stack] == [0, 1]
    assert [type(s) for s in loaded.plan] == [LandStep, WorkflowStep, LandStep]


def test_load_state_version_mismatch(tmp_path) -> None:  # noqa: ANN001
    sf = tmp_path / "state.json"
    sf.write_text('{"version": 999, "stack": [], "plan": [], "branch": "x", "base": "y"}')
    with pytest.raises(ValueError, match="Unsupported state file version"):
        AutolandCheckpointer.load(sf)
