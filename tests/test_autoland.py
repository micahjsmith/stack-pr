import argparse
import configparser
import dataclasses
import json
import os
import shlex
import subprocess
import sys
import unicodedata
from pathlib import Path
from unittest.mock import Mock

import pytest

from stack_pr import autoland
from stack_pr.autoland import (
    _ASCII_GLYPHS,
    _ICON_CELLS,
    _POINTER_CELLS,
    _RE_MARKUP,
    _UNICODE_GLYPHS,
    PLAN_COMMENT_COLUMN,
    AutolandCheckpointer,
    AutolandLock,
    AutolandOptions,
    CheckStatus,
    ConfirmStep,
    LandingContext,
    LandStep,
    StackEntry,
    WorkflowStep,
    _ask_replan_or_overwrite,
    _describe_step,
    _next_steps_lines,
    _pick_glyphs,
    _PlainConsole,
    _plan_rows,
    _replan,
    _run_fresh,
    _run_resume,
    _StepRow,
    _stop_running_autoland,
    carry_over_progress,
    evaluate_checks,
    format_plan_for_editor,
    generate_default_plan,
    parse_plan,
    plan_from_file,
)
from tests.helpers import common_args, git, init_repo


def _args(**overrides) -> argparse.Namespace:  # noqa: ANN003
    base = {
        "poll_interval": None,
        "max_check_retries": None,
        "max_queue_retries": None,
        "workflow_timeout": None,
        "count": None,
        "dry_run": False,
        "branch": None,
        "interactive": False,
        "resume": False,
        "state_file": None,
        "always_cleanup": False,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def _parsed_opts(**overrides) -> AutolandOptions:  # noqa: ANN003
    """Options as parsed from an empty config and the given CLI flags."""
    return AutolandOptions.from_config_and_args(
        configparser.ConfigParser(), _args(**overrides)
    )


def _opts(**overrides) -> AutolandOptions:  # noqa: ANN003
    """Options built directly, with every wait and retry turned off."""
    base = {
        "merge_queue": True,
        "required_checks": [],
        "poll_interval": 0,
        "max_check_retries": 0,
        "max_queue_retries": 0,
        "merge_timeout": 0,
        "workflow_timeout": 3600,
        "default_workflow": None,
        "count": None,
        "dry_run": False,
        "branch": None,
        "interactive": False,
        "resume": False,
        "state_file": None,
        "always_cleanup": False,
    }
    base.update(overrides)
    return AutolandOptions(**base)


# --- options -------------------------------------------------------------


def test_options_precedence_flag_over_config_over_default() -> None:
    cfg = configparser.ConfigParser()
    cfg.add_section("autoland")
    cfg.set("autoland", "merge_queue", "true")
    cfg.set("autoland", "poll_interval", "99")
    cfg.set("autoland", "required_checks", "a, b ,c")

    opts = AutolandOptions.from_config_and_args(cfg, _args(max_check_retries=7))

    assert opts.merge_queue is True
    assert opts.poll_interval == 99  # from config
    assert opts.max_check_retries == 7  # from flag
    assert opts.max_queue_retries == autoland.DEFAULT_MAX_QUEUE_RETRIES  # default
    assert opts.required_checks == ["a", "b", "c"]


def test_default_workflow_from_config() -> None:
    cfg = configparser.ConfigParser()
    cfg.add_section("autoland")
    cfg.set("autoland", "default_workflow", "deploy.yaml")
    opts = AutolandOptions.from_config_and_args(cfg, _args())
    assert opts.default_workflow == "deploy.yaml"


def test_default_workflow_absent_is_none() -> None:
    cfg = configparser.ConfigParser()
    cfg.add_section("autoland")
    # Empty/whitespace-only value is treated as unset.
    cfg.set("autoland", "default_workflow", "  ")
    opts = AutolandOptions.from_config_and_args(cfg, _args())
    assert opts.default_workflow is None


# --- merge-queue gate ----------------------------------------------------


def test_run_autoland_requires_merge_queue() -> None:
    cfg = configparser.ConfigParser()  # no [autoland] -> merge_queue False
    with pytest.raises(NotImplementedError):
        autoland.run_autoland(common_args(), _args(), cfg)


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


def test_poll_merge_lookup_failure_is_not_booted(mocker) -> None:  # noqa: ANN001
    # A transient failure looking up the merge-queue entry says nothing about
    # whether the PR is still queued, so it must not count as being booted.
    mocker.patch.object(autoland.github, "pr_state", return_value="OPEN")
    mocker.patch.object(autoland.github, "owner_repo", return_value=("o", "r"))
    mocker.patch.object(autoland, "run", side_effect=RuntimeError("HTTP 502"))
    res = autoland.github.poll_merge(1)
    assert not res.merged
    assert not res.booted
    assert not res.error


# --- workflow checkpoint SHA ---------------------------------------------


def test_merge_commit_parses_oid(mocker) -> None:  # noqa: ANN001
    mocker.patch.object(
        autoland, "gh_json", return_value={"mergeCommit": {"oid": "deadbeef"}}
    )
    assert autoland.github.merge_commit(1) == "deadbeef"


def test_merge_commit_none_when_unmerged(mocker) -> None:  # noqa: ANN001
    mocker.patch.object(autoland, "gh_json", return_value={"mergeCommit": None})
    assert autoland.github.merge_commit(1) is None


def test_refresh_last_landed_sha_prefers_merge_commit(mocker) -> None:  # noqa: ANN001
    # origin/<target> HEAD has advanced past our merge commit (bot commits,
    # other PRs). We must record OUR merge commit, not the moving HEAD.
    mocker.patch.object(autoland, "run")  # git fetch is a no-op
    mocker.patch.object(autoland.github, "merge_commit", return_value="mergesha")
    ctx = LandingContext(last_landed_sha="")
    autoland._refresh_last_landed_sha(ctx, common_args(), pr_number=42)  # noqa: SLF001
    assert ctx.last_landed_sha == "mergesha"


def test_refresh_last_landed_sha_falls_back_to_head(mocker) -> None:  # noqa: ANN001
    # No PR context (e.g. resume) or the merge commit is unknown: fall back to
    # origin/<target> HEAD.
    mocker.patch.object(
        autoland,
        "run",
        return_value=argparse.Namespace(stdout="headsha\n", returncode=0),
    )
    mocker.patch.object(autoland.github, "merge_commit", return_value=None)
    ctx = LandingContext(last_landed_sha="")
    autoland._refresh_last_landed_sha(ctx, common_args(), pr_number=42)  # noqa: SLF001
    assert ctx.last_landed_sha == "headsha"


def test_wait_for_workflow_accepts_run_on_merge_commit(mocker) -> None:  # noqa: ANN001
    # Regression: a green deploy run on our exact merge commit must satisfy the
    # checkpoint even though origin/<target> has since moved on.
    mocker.patch.object(
        autoland.github,
        "workflow_runs",
        return_value=[
            {"headSha": "mergesha", "status": "completed", "conclusion": "success"}
        ],
    )
    step = WorkflowStep(workflow="deploy.yaml")
    ctx = LandingContext(last_landed_sha="mergesha")
    assert autoland.wait_for_workflow(step, opts=_opts(), common=common_args(), ctx=ctx)
    assert step.state == "succeeded"


def test_wait_for_workflow_fetches_when_run_sha_is_unknown_locally(mocker) -> None:  # noqa: ANN001
    # Regression: origin/<target> had advanced past our last fetch, so the
    # completed run's head commit was not in this clone. `git merge-base
    # --is-ancestor` then exits 128 ("Not a valid commit name"), which used to
    # read as "not an ancestor" and made the checkpoint poll forever. A fetch
    # must resolve it.
    mocker.patch.object(
        autoland.github,
        "workflow_runs",
        return_value=[
            {"headSha": "newersha", "status": "completed", "conclusion": "success"}
        ],
    )
    calls: list[list] = []

    def _run(cmd, **_kwargs):  # noqa: ANN001, ANN003, ANN202
        calls.append(cmd)
        if cmd[:3] == ["git", "merge-base", "--is-ancestor"]:
            # Unknown commit before the fetch, a genuine answer after it.
            fetched = ["git", "fetch", "origin", "main"] in calls
            return argparse.Namespace(stdout="", returncode=0 if fetched else 128)
        return argparse.Namespace(stdout="", returncode=0)

    mocker.patch.object(autoland, "run", side_effect=_run)
    contains = mocker.patch.object(autoland.github, "contains")
    step = WorkflowStep(workflow="deploy.yaml")
    ctx = LandingContext(last_landed_sha="mergesha")
    assert autoland.wait_for_workflow(step, opts=_opts(), common=common_args(), ctx=ctx)
    assert step.state == "succeeded"
    assert ["git", "fetch", "origin", "main"] in calls
    contains.assert_not_called()  # the fetch answered it; no API call needed


def test_wait_for_workflow_falls_back_to_github_compare(mocker) -> None:  # noqa: ANN001
    # The run's head commit is still missing after the fetch (e.g. a
    # merge-queue commit that never landed on the target branch): ask GitHub.
    mocker.patch.object(
        autoland.github,
        "workflow_runs",
        return_value=[
            {"headSha": "queuesha", "status": "completed", "conclusion": "success"}
        ],
    )
    mocker.patch.object(
        autoland,
        "run",
        return_value=argparse.Namespace(stdout="", returncode=128),
    )
    contains = mocker.patch.object(autoland.github, "contains", return_value=True)
    step = WorkflowStep(workflow="deploy.yaml")
    ctx = LandingContext(last_landed_sha="mergesha")
    assert autoland.wait_for_workflow(step, opts=_opts(), common=common_args(), ctx=ctx)
    contains.assert_called_once_with("mergesha", "queuesha")


def test_wait_for_workflow_keeps_waiting_when_ancestry_unknown(mocker) -> None:  # noqa: ANN001
    # Neither git nor GitHub can answer: warn and keep waiting rather than
    # treating "unknown" as "the deploy included our code".
    def _runs(*_a, **_k) -> list:  # noqa: ANN002, ANN003
        ctx.aborted = True  # stop after one poll
        return [{"headSha": "mystery", "status": "completed", "conclusion": "success"}]

    mocker.patch.object(autoland.github, "workflow_runs", side_effect=_runs)
    mocker.patch.object(
        autoland,
        "run",
        return_value=argparse.Namespace(stdout="", returncode=128),
    )
    mocker.patch.object(autoland.github, "contains", return_value=None)
    mocker.patch.object(autoland, "resilient_sleep", return_value=0.0)
    step = WorkflowStep(workflow="deploy.yaml")
    ctx = LandingContext(last_landed_sha="mergesha")
    assert not autoland.wait_for_workflow(
        step, opts=_opts(), common=common_args(), ctx=ctx
    )
    assert step.state != "succeeded"


def test_local_is_ancestor_distinguishes_no_from_unknown(mocker) -> None:  # noqa: ANN001
    run_mock = mocker.patch.object(autoland, "run")
    run_mock.return_value = argparse.Namespace(stdout="", returncode=0)
    assert autoland._local_is_ancestor("a", "b") is True  # noqa: SLF001
    run_mock.return_value = argparse.Namespace(stdout="", returncode=1)
    assert autoland._local_is_ancestor("a", "b") is False  # noqa: SLF001
    run_mock.return_value = argparse.Namespace(stdout="", returncode=128)
    assert autoland._local_is_ancestor("a", "b") is None  # noqa: SLF001


def test_sha_eq_requires_a_long_enough_prefix() -> None:
    assert autoland._sha_eq("abcdef1234", "abcdef1")  # noqa: SLF001
    assert not autoland._sha_eq("abcdef1234", "abcdef2")  # noqa: SLF001
    # Below git's minimum abbreviation a prefix does not identify a commit, so
    # matching on it could satisfy a checkpoint with the wrong deploy.
    assert not autoland._sha_eq("abcdef", "abcdef1234")  # noqa: SLF001
    assert not autoland._sha_eq("", "abcdef1234")  # noqa: SLF001


def test_github_contains_uses_merge_base(mocker) -> None:  # noqa: ANN001
    mocker.patch.object(autoland.github, "_owner_repo", ("o", "r"))
    run_mock = mocker.patch.object(autoland, "run")
    run_mock.return_value = argparse.Namespace(stdout="basesha\n", returncode=0)
    assert autoland.github.contains("basesha", "headsha") is True
    run_mock.return_value = argparse.Namespace(stdout="othersha\n", returncode=0)
    assert autoland.github.contains("basesha", "headsha") is False
    run_mock.return_value = argparse.Namespace(stdout="null\n", returncode=0)
    assert autoland.github.contains("basesha", "headsha") is None
    run_mock.side_effect = RuntimeError("HTTP 404")
    assert autoland.github.contains("basesha", "headsha") is None


def test_ancestry_caches_verdicts(mocker) -> None:  # noqa: ANN001
    # The poll loop re-examines the same runs every interval; commits are
    # immutable, so each pair is decided at most once.
    contains = mocker.patch.object(autoland.github, "contains", return_value=False)
    mocker.patch.object(
        autoland,
        "run",
        return_value=argparse.Namespace(stdout="", returncode=128),
    )
    ancestry = autoland._Ancestry(common_args())  # noqa: SLF001
    assert ancestry.contains("landedsha", "runsha1") is False
    assert ancestry.contains("landedsha", "runsha1") is False
    assert ancestry.contains("landedsha", "runsha2") is False
    # One GitHub request per distinct pair, however often it is asked about.
    assert [c.args for c in contains.call_args_list] == [
        ("landedsha", "runsha1"),
        ("landedsha", "runsha2"),
    ]


def test_wait_for_workflow_ignores_failed_and_incomplete(mocker) -> None:  # noqa: ANN001
    # A failed run and a still-running run on our SHA must not satisfy the
    # checkpoint; abort so the poll loop terminates for the test.
    calls = {"n": 0}

    def _runs(*_a, **_k) -> list:  # noqa: ANN002, ANN003
        calls["n"] += 1
        ctx.aborted = True  # stop after one poll
        return [
            {"headSha": "mergesha", "status": "completed", "conclusion": "failure"},
            {"headSha": "mergesha", "status": "in_progress", "conclusion": None},
        ]

    mocker.patch.object(autoland.github, "workflow_runs", side_effect=_runs)
    mocker.patch.object(autoland, "resilient_sleep", return_value=0.0)
    step = WorkflowStep(workflow="deploy.yaml")
    ctx = LandingContext(last_landed_sha="mergesha")
    assert not autoland.wait_for_workflow(
        step, opts=_opts(), common=common_args(), ctx=ctx
    )


# --- plan parsing --------------------------------------------------------


def _stack(n: int) -> list:
    return [StackEntry(pr_url=f"u/{i}", pr_number=i, branch=f"b{i}") for i in range(n)]


def test_parse_plan_with_workflow_and_confirm() -> None:
    text = "l\nw deploy.yaml\nc QA sign-off complete\nl\n"
    steps = parse_plan(text, _stack(2))
    assert [type(s) for s in steps] == [LandStep, WorkflowStep, ConfirmStep, LandStep]
    assert steps[1].workflow == "deploy.yaml"
    assert steps[2].condition == "QA sign-off complete"
    assert [s.entry_index for s in steps if isinstance(s, LandStep)] == [0, 1]


def test_parse_plan_bare_confirm_has_no_condition() -> None:
    steps = parse_plan("l\nc\n", _stack(1))
    assert [type(s) for s in steps] == [LandStep, ConfirmStep]
    assert steps[1].condition == ""


def test_parse_plan_rejects_old_deploy_letter() -> None:
    # The 'd' letter was renamed to 'w'; it should no longer be recognized.
    with pytest.raises(ValueError, match="unrecognized step"):
        parse_plan("l\nd deploy.yaml\n", _stack(1))


def test_generate_default_plan_appends_workflow_when_configured() -> None:
    plain = generate_default_plan(_stack(2))
    assert [type(s) for s in plain] == [LandStep, LandStep]

    with_wf = generate_default_plan(_stack(2), default_workflow="deploy.yaml")
    assert [type(s) for s in with_wf] == [LandStep, LandStep, WorkflowStep]
    assert with_wf[-1].workflow == "deploy.yaml"


def test_generate_default_plan_count_lands_bottom_n() -> None:
    # count lands only the bottom N PRs (a prefix of the stack).
    plan = generate_default_plan(_stack(4), count=2)
    assert [type(s) for s in plan] == [LandStep, LandStep]
    assert [s.entry_index for s in plan] == [0, 1]


def test_parse_plan_allows_partial_land() -> None:
    # Landing only the bottom PR of a larger stack is now allowed.
    steps = parse_plan("l\n", _stack(3))
    assert [type(s) for s in steps] == [LandStep]
    assert steps[0].entry_index == 0


def test_parse_plan_rejects_no_land_steps() -> None:
    with pytest.raises(ValueError, match="nothing to land"):
        parse_plan("c hold\n", _stack(2))


def test_parse_plan_rejects_too_many_lands() -> None:
    with pytest.raises(ValueError, match="too many 'l' steps"):
        parse_plan("l\nl\nl\n", _stack(2))


def test_parse_plan_rejects_unknown_step() -> None:
    with pytest.raises(ValueError, match="unrecognized step"):
        parse_plan("frobnicate\n", _stack(1))


# --- pinned land steps ---------------------------------------------------


def _pinned_stack(numbers: list[int]) -> list:
    return [
        StackEntry(pr_url=f"u/{n}", pr_number=n, branch=f"b{n}", title=f"PR {n}")
        for n in numbers
    ]


def _never_merged(_pr: int) -> bool:
    return False


def test_parse_plan_pins_land_steps_to_named_prs() -> None:
    steps = parse_plan("l 101\nl 102\n", _pinned_stack([101, 102]))
    assert [s.entry_index for s in steps] == [0, 1]
    assert [s.pr_number for s in steps] == [101, 102]
    assert not any(s.already_landed for s in steps)


@pytest.mark.parametrize(
    "ref",
    [
        "101",
        "https://github.com/user/repo/pull/101",
        "https://github.com/USER/Repo/pull/101",  # owner/repo are case-insensitive
        "https://github.com/user/repo/pull/101/",
    ],
)
def test_parse_plan_accepts_pr_reference_forms(ref: str, mocker) -> None:  # noqa: ANN001
    mocker.patch.object(autoland.github, "owner_repo", return_value=("user", "repo"))
    steps = parse_plan(f"l {ref}\n", _pinned_stack([101]))
    assert steps[0].pr_number == 101
    assert steps[0].entry_index == 0


def test_parse_plan_rejects_pr_url_from_another_repo(mocker) -> None:  # noqa: ANN001
    # The number would resolve against *this* repo, landing an unrelated PR.
    mocker.patch.object(autoland.github, "owner_repo", return_value=("user", "repo"))
    with pytest.raises(ValueError, match="across repositories is not currently"):
        parse_plan(
            "l https://github.com/other/project/pull/101\n", _pinned_stack([101])
        )


def test_parse_plan_rejects_duplicate_pinned_pr() -> None:
    # Regression: this used to index past the stack and raise IndexError, which
    # no caller catches.
    with pytest.raises(ValueError, match="already landed by an earlier 'l' step"):
        parse_plan("l 101\nl 101\n", _pinned_stack([101]))


def test_parse_plan_rejects_duplicate_pinned_pr_mid_stack() -> None:
    with pytest.raises(ValueError, match="already landed by an earlier 'l' step"):
        parse_plan("l 101\nl 102\nl 101\n", _pinned_stack([101, 102, 103]))


def test_parse_plan_treats_hash_pr_reference_as_a_comment() -> None:
    # '#' starts a comment, so 'l #102' cannot pin a PR — it is a bare 'l'
    # with a trailing comment, and lands the next PR in the stack.
    steps = parse_plan("l #102\n", _pinned_stack([101, 102]))
    assert steps[0].entry_index == 0
    assert steps[0].pr_number == 101


def test_parse_plan_skips_land_steps_whose_prs_already_landed() -> None:
    # #101 and #102 have merged and been rebased away, so only #103 remains in
    # the stack — but the plan that named all three still has to work.
    steps = parse_plan(
        "l 101\nc QA sign-off complete\nl 102\nl 103\n",
        _pinned_stack([103]),
        pr_is_merged=lambda pr: pr in (101, 102),
    )
    lands = [s for s in steps if isinstance(s, LandStep)]
    assert [s.already_landed for s in lands] == [True, True, False]
    assert [s.pr_number for s in lands] == [101, 102, 103]
    assert lands[-1].entry_index == 0


def test_parse_plan_marks_steps_before_the_landed_prefix_done() -> None:
    # A workflow and a confirmation sandwiched between two landed PRs must have
    # happened already — re-running the plan shouldn't ask for them again.
    steps = parse_plan(
        "l 101\nw deploy.yaml\nc QA sign-off complete\nl 102\nw deploy.yaml\nl 103\n",
        _pinned_stack([103]),
        pr_is_merged=lambda pr: pr in (101, 102),
    )
    assert steps[1].state == "skipped"
    assert steps[2].confirmed is True
    # The workflow *after* the last landed PR still has to run.
    assert steps[4].state == "pending"


def test_parse_plan_marks_nothing_done_when_nothing_has_landed() -> None:
    # Regression: with no landed steps there is no completed prefix, and the
    # workflow/confirmation steps must all still run.
    steps = parse_plan(
        "l 101\nw deploy.yaml\nc QA sign-off complete\nl 102\n",
        _pinned_stack([101, 102]),
    )
    assert steps[1].state == "pending"
    assert steps[2].confirmed is False


def test_parse_plan_rejects_pinned_pr_that_is_neither_open_nor_merged() -> None:
    with pytest.raises(ValueError, match="not in the stack and has not been merged"):
        parse_plan("l 999\n", _pinned_stack([101]), pr_is_merged=_never_merged)


def test_parse_plan_rejects_land_step_out_of_stack_order() -> None:
    # The plan wants #102 next, but #101 is still below it in the stack.
    with pytest.raises(ValueError, match=r"lands PR #102 next.*stack is #101"):
        parse_plan("l 102\n", _pinned_stack([101, 102]), pr_is_merged=_never_merged)


def test_parse_plan_rejects_landed_step_after_a_still_open_one() -> None:
    # #102 merged out of order, ahead of #101 which the plan lands first.
    with pytest.raises(ValueError, match=r"#102 has already merged.*after PR #101"):
        parse_plan(
            "l 101\nl 102\n",
            _pinned_stack([101]),
            pr_is_merged=lambda pr: pr == 102,
        )


def test_parse_plan_rejects_malformed_pr_reference() -> None:
    with pytest.raises(ValueError, match="takes a PR number or URL"):
        parse_plan("l next-one\n", _pinned_stack([101]))


def test_parse_plan_pinned_and_bare_land_steps_can_mix() -> None:
    # A bare 'l' keeps taking the next stack entry, pinned or not.
    steps = parse_plan("l 101\nl\n", _pinned_stack([101, 102]))
    assert [s.entry_index for s in steps] == [0, 1]
    assert [s.pr_number for s in steps] == [101, 102]


def test_parse_plan_land_steps_survive_a_fully_landed_prefix() -> None:
    # Nothing left to land, but a trailing workflow still needs to be waited on.
    steps = parse_plan(
        "l 101\nw deploy.yaml\n",
        _pinned_stack([102]),
        pr_is_merged=lambda pr: pr == 101,
    )
    assert steps[0].already_landed
    assert steps[1].state == "pending"


def test_parse_plan_consults_github_for_unknown_prs(mocker) -> None:  # noqa: ANN001
    pr_state = mocker.patch.object(autoland.github, "pr_state", return_value="MERGED")
    steps = parse_plan("l 101\nl 102\n", _pinned_stack([102]))
    assert steps[0].already_landed
    pr_state.assert_called_once_with(101)


# --- plan from file ------------------------------------------------------


def test_interactive_plan_file_carries_the_plan_suffix(mocker) -> None:  # noqa: ANN001
    # The file handed to $EDITOR is a plan, so it is named like one: editors
    # key off .autoland-plan to highlight the syntax.
    seen = {}

    def _editor(cmd, **_kwargs):  # noqa: ANN001, ANN003, ANN202
        seen["path"] = cmd[-1]
        Path(cmd[-1]).write_text("l\n")
        return argparse.Namespace(returncode=0)

    mocker.patch.object(autoland.subprocess, "run", side_effect=_editor)

    autoland.edit_plan_interactive(_stack(1))

    assert seen["path"].endswith(".autoland-plan")


def test_interactive_plan_editor_may_carry_arguments(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    # $EDITOR is a shell-style command line ("code --wait", "vim -u NONE"),
    # not just an executable name: its arguments must reach the editor.
    script = tmp_path / "fake editor.py"
    script.write_text(
        "import sys\n"
        "from pathlib import Path\n"
        "args = sys.argv[1:]\n"
        "assert args[:2] == ['--wait', 'two words'], args\n"
        "Path(args[-1]).write_text('l\\nw deploy.yaml\\n')\n"
    )
    editor = shlex.join([sys.executable, str(script), "--wait", "two words"])
    monkeypatch.setenv("EDITOR", editor)

    steps = autoland.edit_plan_interactive(_stack(1))

    assert [type(s) for s in steps] == [LandStep, WorkflowStep]
    assert steps[1].workflow == "deploy.yaml"


def test_editor_format_pins_pr_numbers() -> None:
    stack = _pinned_stack([101, 102])
    text = format_plan_for_editor(stack, generate_default_plan(stack))
    # The PR title rides along as a trailing comment, aligned at the plan's
    # comment column so the titles line up whatever the PR numbers are.
    assert "l 101".ljust(PLAN_COMMENT_COLUMN) + "# PR 101" in text
    assert "l 102".ljust(PLAN_COMMENT_COLUMN) + "# PR 102" in text


def test_editor_format_round_trips_through_plan_from_file(tmp_path) -> None:  # noqa: ANN001
    # The file format is exactly what the $EDITOR shows: rendering the default
    # plan and loading it back must reproduce the same steps.
    stack = _stack(2)
    plan = generate_default_plan(stack, default_workflow="deploy.yaml")
    path = tmp_path / "plan.txt"
    path.write_text(format_plan_for_editor(stack, plan))

    loaded = plan_from_file(path, stack)
    assert [type(s) for s in loaded] == [type(s) for s in plan]
    assert [s.entry_index for s in loaded if isinstance(s, LandStep)] == [0, 1]
    assert loaded[-1].workflow == "deploy.yaml"


def test_plan_from_file_parses_hand_written_plan(tmp_path) -> None:  # noqa: ANN001
    path = tmp_path / "plan.txt"
    path.write_text(
        "# a hand-written plan\n"
        "l          # PR #0\n"
        "w deploy.yaml\n"
        "c QA sign-off complete\n"
        "\n"
        "l\n"
    )
    steps = plan_from_file(path, _stack(2))
    assert [type(s) for s in steps] == [LandStep, WorkflowStep, ConfirmStep, LandStep]
    assert steps[1].workflow == "deploy.yaml"
    assert steps[2].condition == "QA sign-off complete"


@pytest.mark.usefixtures("autoland_console")
def test_plan_from_file_missing_file_exits(tmp_path) -> None:  # noqa: ANN001
    with pytest.raises(SystemExit) as exc:
        plan_from_file(tmp_path / "nope.txt", _stack(1))
    assert exc.value.code == 1


@pytest.mark.usefixtures("autoland_console")
def test_plan_from_file_invalid_content_exits(tmp_path) -> None:  # noqa: ANN001
    path = tmp_path / "plan.txt"
    path.write_text("frobnicate\n")
    with pytest.raises(SystemExit) as exc:
        plan_from_file(path, _stack(1))
    assert exc.value.code == 1


def test_plan_file_arg_parses_and_is_exclusive_with_interactive() -> None:
    import configparser  # noqa: PLC0415

    from stack_pr import cli  # noqa: PLC0415

    parser = cli.create_argparser(configparser.ConfigParser())
    args = parser.parse_args(["autoland", "--plan-file", "plan.txt"])
    assert str(args.plan_file) == "plan.txt"

    # -i and --plan-file both set the plan source, so they're mutually exclusive.
    with pytest.raises(SystemExit):
        parser.parse_args(["autoland", "-i", "--plan-file", "plan.txt"])


def test_from_config_and_args_resolves_plan_file_to_absolute() -> None:
    opts = AutolandOptions.from_config_and_args(
        configparser.ConfigParser(), _args(plan_file="plan.txt")
    )
    assert opts.plan_file is not None
    assert opts.plan_file.is_absolute()


def _merge_queue_cfg() -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    cfg.add_section("autoland")
    cfg.set("autoland", "merge_queue", "true")
    return cfg


@pytest.mark.usefixtures("autoland_console")
def test_run_autoland_rejects_plan_file_with_count() -> None:
    with pytest.raises(SystemExit) as exc:
        autoland.run_autoland(
            common_args(), _args(plan_file="plan.txt", count=2), _merge_queue_cfg()
        )
    assert exc.value.code == 1


@pytest.mark.usefixtures("autoland_console")
def test_run_autoland_rejects_plan_file_with_resume() -> None:
    with pytest.raises(SystemExit) as exc:
        autoland.run_autoland(
            common_args(), _args(plan_file="plan.txt", resume=True), _merge_queue_cfg()
        )
    assert exc.value.code == 1


# --- confirm next-steps preview ------------------------------------------


def test_describe_step_variants() -> None:
    stack = [StackEntry(pr_url="u", pr_number=5, branch="b", title="Scale to [1,25]")]
    ctx = LandingContext(stack=stack)
    assert _describe_step(LandStep(entry_index=0), ctx) == "Land PR #5: Scale to [1,25]"
    assert (
        _describe_step(WorkflowStep(workflow="deploy.yaml"), ctx)
        == "Wait for workflow deploy.yaml"
    )
    assert (
        _describe_step(ConfirmStep(condition="QA done"), ctx)
        == "Manual confirmation: QA done"
    )
    assert _describe_step(ConfirmStep(), ctx) == "Manual confirmation"


def test_describe_step_untitled_land() -> None:
    ctx = LandingContext(stack=[StackEntry(pr_url="u", pr_number=9, branch="b")])
    assert _describe_step(LandStep(entry_index=0), ctx) == "Land PR #9: (untitled)"


def test_describe_step_already_landed_land() -> None:
    # No stack entry to read a title from — the step still has to describe itself.
    ctx = LandingContext(stack=_stack(1))
    step = LandStep(entry_index=-1, pr_number=101)
    assert _describe_step(step, ctx) == "Land PR #101 (already landed)"


# --- plan display ---------------------------------------------------------


def _partly_landed_ctx() -> LandingContext:
    """A plan whose first two PRs landed in an earlier run, replayed today."""
    stack = _pinned_stack([103])
    plan = parse_plan(
        "l 101\nw deploy.yaml\nc QA sign-off complete\nl 102\nw deploy.yaml\nl 103\n",
        stack,
        pr_is_merged=lambda pr: pr in (101, 102),
    )
    return LandingContext(stack=stack, plan=plan)


def _rows_by_number(ctx: LandingContext) -> dict[str, _StepRow]:
    return {row.number: row for row in _plan_rows(ctx)}


def test_landed_prefix_end_finds_the_last_already_landed_step() -> None:
    plan = _partly_landed_ctx().plan
    assert autoland.landed_prefix_end(plan) == 3  # the 'l 102' step


def test_landed_prefix_end_is_negative_when_nothing_landed() -> None:
    # -1, not 0: callers slice with it, and 0 would wrongly name step 1.
    assert autoland.landed_prefix_end([LandStep(entry_index=0), ConfirmStep()]) == -1


def test_status_says_assumed_completed_inside_the_landed_prefix() -> None:
    # The workflow and confirmation between #101 and #102 could not have been
    # outstanding while those PRs merged, so the plan credits them as done
    # rather than showing them as pending work.
    rows = _rows_by_number(_partly_landed_ctx())
    assert rows["2."].status == "Assumed completed"
    assert rows["3."].status == "Assumed completed"
    # The workflow *after* the landed prefix still has to run.
    assert rows["5."].status == "Pending"


def test_status_distinguishes_a_real_confirmation_from_an_assumed_one() -> None:
    ctx = LandingContext(
        stack=_pinned_stack([101]),
        plan=[ConfirmStep(condition="QA", confirmed=True), LandStep(entry_index=0)],
    )
    assert _rows_by_number(ctx)["1."].status == "Confirmed"


def test_pointer_starts_after_the_landed_prefix() -> None:
    # Regression: the pointer sat on step 1, implying autoland was about to
    # re-land a PR that merged in an earlier run.
    rows = _plan_rows(_partly_landed_ctx())
    assert [r.number for r in rows if r.is_next] == ["5."]


def test_pointer_follows_current_step_once_a_run_is_under_way() -> None:
    ctx = _partly_landed_ctx()
    ctx.current_step = 5
    rows = _plan_rows(ctx)
    assert [r.number for r in rows if r.is_next] == ["6."]


def test_pointer_starts_at_step_one_when_nothing_has_landed() -> None:
    stack = _pinned_stack([101, 102])
    ctx = LandingContext(stack=stack, plan=parse_plan("l 101\nl 102\n", stack))
    rows = _plan_rows(ctx)
    assert [r.number for r in rows if r.is_next] == ["1."]


def test_land_row_detail_omits_retries_until_something_is_retried() -> None:
    stack = _pinned_stack([101])
    stack[0].error_message = "Checks in progress..."
    ctx = LandingContext(stack=stack, plan=[LandStep(entry_index=0, pr_number=101)])
    assert _rows_by_number(ctx)["1."].detail == "Checks in progress..."

    stack[0].check_retries = 2
    assert (
        _rows_by_number(ctx)["1."].detail
        == "Checks in progress... · retries CI 2 / MQ 0"
    )


def test_render_status_plain_shows_already_landed_step() -> None:
    ctx = LandingContext(
        stack=_pinned_stack([102]),
        plan=[LandStep(entry_index=-1, pr_number=101), LandStep(entry_index=0)],
    )
    rendered = autoland.render_status_plain(ctx)
    assert "Landed" in rendered
    assert "| Land" in rendered
    assert "#101" in rendered


def test_render_status_plain_headline_counts_completed_steps() -> None:
    rendered = autoland.render_status_plain(_partly_landed_ctx())
    assert "Autoland plan · 6 steps · 4 done · 2 remaining" in rendered


def test_render_status_plain_reports_an_abort() -> None:
    ctx = _partly_landed_ctx()
    ctx.aborted = True
    ctx.abort_reason = "PR #103 checks failed"
    assert "ABORTED: PR #103 checks failed" in autoland.render_status_plain(ctx)


@pytest.mark.skipif(not autoland.HAVE_RICH, reason="requires the rich extra")
def test_render_status_rich_handles_already_landed_step() -> None:
    # The already-landed step has no stack entry behind it; rendering it must
    # not reach into ctx.stack.
    ctx = LandingContext(
        stack=_pinned_stack([102]),
        plan=[LandStep(entry_index=-1, pr_number=101), LandStep(entry_index=0)],
    )
    rendered = autoland.render_status_rich(ctx)
    from rich.console import Console as RichConsole  # noqa: PLC0415

    with RichConsole(record=True, width=100) as rich_console:
        rich_console.print(rendered)
    text = rich_console.export_text()
    assert "#101" in text
    assert "Landed" in text


@pytest.mark.skipif(not autoland.HAVE_RICH, reason="requires the rich extra")
def test_render_status_rich_does_not_parse_a_pr_title_as_markup() -> None:
    stack = [StackEntry(pr_url="u", pr_number=7, branch="b", title="Scale to [1,25]")]
    ctx = LandingContext(stack=stack, plan=[LandStep(entry_index=0, pr_number=7)])
    from rich.console import Console as RichConsole  # noqa: PLC0415

    with RichConsole(record=True, width=100) as rich_console:
        rich_console.print(autoland.render_status_rich(ctx))
    assert "Scale to [1,25]" in rich_console.export_text()


# --- display: encoding, alignment, and truncation -------------------------


def test_plain_console_keeps_bracketed_text_that_is_not_a_style() -> None:
    # Regression: the markup stripper ate any bracketed lowercase word, so a PR
    # titled "[wip] make it fast" lost its tag on the no-rich path.
    assert _RE_MARKUP.sub("", "[bold red]x[/bold red] [wip] [1,25]") == (
        "x [wip] [1,25]"
    )


def test_ascii_glyphs_are_used_when_stdout_cannot_encode_emoji(mocker) -> None:  # noqa: ANN001
    mocker.patch.object(autoland.sys, "stdout", mocker.Mock(encoding="cp1252"))
    assert _pick_glyphs() is _ASCII_GLYPHS


def test_unicode_glyphs_are_used_on_a_utf8_stdout(mocker) -> None:  # noqa: ANN001
    mocker.patch.object(autoland.sys, "stdout", mocker.Mock(encoding="utf-8"))
    assert _pick_glyphs() is _UNICODE_GLYPHS


def test_plain_status_is_encodable_on_a_legacy_code_page(mocker) -> None:  # noqa: ANN001
    # Printing an un-encodable glyph raises UnicodeEncodeError, and print_status
    # runs from the SIGINT handler and from _finish — so even a successful run
    # would die on the way out, stranding its checkpoint and worktree.
    mocker.patch.object(autoland, "GLYPHS", _ASCII_GLYPHS)
    autoland.render_status_plain(_partly_landed_ctx()).encode("cp1252")


def _cell_len(text: str) -> int:
    """Display width of *text* in terminal cells.

    rich measures this itself; without the extra installed, fall back to the
    East Asian width table it derives from, so the alignment tests still run on
    the no-rich path — where len() undercounts every wide glyph by one.
    """
    if autoland.HAVE_RICH:
        from rich.cells import cell_len  # noqa: PLC0415

        return cell_len(text)
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)


@pytest.mark.parametrize(
    "glyphs",
    [_UNICODE_GLYPHS, _ASCII_GLYPHS],
    ids=["unicode", "ascii"],
)
def test_every_icon_occupies_the_width_the_layout_assumes(glyphs) -> None:  # noqa: ANN001
    # The columns after the icon are aligned by padding alone, so an icon of the
    # wrong display width shifts every field on that row.
    for icon in (glyphs.done, glyphs.active, glyphs.pending, glyphs.failed):
        assert _cell_len(icon) == _ICON_CELLS
    assert _cell_len(glyphs.pointer) == _POINTER_CELLS


def test_detail_line_hangs_under_the_status_column() -> None:
    # Regression: the indent was computed with len() over a two-cell emoji, so
    # every detail line sat one column left of the status it belongs to.
    stack = _pinned_stack([101])
    stack[0].error_message = "boom"
    ctx = LandingContext(stack=stack, plan=[LandStep(entry_index=0, pr_number=101)])
    header, detail = autoland.render_status_plain(ctx).splitlines()[3:5]

    status = _rows_by_number(ctx)["1."].status
    assert _cell_len(header[: header.index(status)]) == _cell_len(
        detail[: len(detail) - len(detail.lstrip())]
    )


@pytest.mark.skipif(not autoland.HAVE_RICH, reason="requires the rich extra")
def test_rich_status_truncates_titles_but_never_the_failure_reason() -> None:
    # A piped autoland gets rich's default 80-column width; the abort reason and
    # the per-step error are the whole point of the output when a land fails.
    stack = _pinned_stack([101])
    stack[0].title = "A very long pull request title that will not fit " * 3
    stack[0].error_message = (
        "Checks failed after 3 retries: build-and-test, lint, typecheck, "
        "integration-tests, docs, and a few more besides"
    )
    ctx = LandingContext(stack=stack, plan=[LandStep(entry_index=0, pr_number=101)])
    ctx.aborted = True
    ctx.abort_reason = (
        "Rebase failed after merging #101: Command failed: git rebase --onto "
        "origin/main abc123 def456 -- error: could not apply def456"
    )
    from rich.console import Console as RichConsole  # noqa: PLC0415

    with RichConsole(record=True, width=80) as rich_console:
        rich_console.print(autoland.render_status_rich(ctx))
    text = rich_console.export_text()

    assert "…" in text  # the title was clipped
    assert ctx.abort_reason in " ".join(text.split())
    assert stack[0].error_message in " ".join(text.split())


def test_next_steps_lines_numbers_remaining_only() -> None:
    stack = [StackEntry(pr_url="u", pr_number=7, branch="b", title="X [1,25] Y")]
    plan = [ConfirmStep(), LandStep(entry_index=0), WorkflowStep(workflow="d.yaml")]
    ctx = LandingContext(stack=stack, plan=plan)

    lines = _next_steps_lines(plan, 0, ctx)

    assert len(lines) == 2
    assert lines[0].startswith("  1. Land PR #7:")
    assert "1,25" in lines[0]  # the title's content survives (escaped or not)
    assert lines[1] == "  2. Wait for workflow d.yaml"


def test_next_steps_lines_empty_for_final_step() -> None:
    plan = [LandStep(entry_index=0), ConfirmStep()]
    ctx = LandingContext(stack=_stack(1), plan=plan)
    assert _next_steps_lines(plan, 1, ctx) == []


# --- executing a partially-landed plan -----------------------------------


@pytest.mark.usefixtures("autoland_console")
def test_execute_plan_skips_landed_prefix_and_targets_last_landed_sha(mocker) -> None:  # noqa: ANN001
    refresh = mocker.patch("stack_pr.autoland._refresh_last_landed_sha")
    wait_for_workflow = mocker.patch(
        "stack_pr.autoland.wait_for_workflow", return_value=True
    )

    stack = _pinned_stack([103])
    plan = parse_plan(
        "l 101\nw deploy.yaml\nc QA sign-off complete\nl 102\nw deploy.yaml\n",
        stack,
        pr_is_merged=lambda pr: pr in (101, 102),
    )
    ctx = LandingContext(stack=stack, plan=plan)
    checkpointer = AutolandCheckpointer(
        path=Path("/dev/null"), branch="feat", base="main"
    )
    mocker.patch.object(checkpointer, "save")

    assert autoland.execute_plan(ctx, common_args(), _opts(), checkpointer) is True

    # Only the trailing workflow runs; the skipped one and the confirmation
    # (which would otherwise block on stdin) are passed over.
    (waited_on,) = [c.args[0] for c in wait_for_workflow.call_args_list]
    assert waited_on is plan[-1]
    # The trailing workflow waits for #102's code: only the last PR of the
    # landed prefix is looked up, not every PR in it.
    pinned = [c.args[2] for c in refresh.call_args_list if len(c.args) > 2]
    assert pinned == [102]


def test_confirm_step_banner_is_a_single_line(mocker, autoland_console) -> None:  # noqa: ANN001
    # Regression: the banner was once built as two list elements, so the join
    # split "Step 1/1: Manual confirmation required" across two lines.
    autoland_console.input.return_value = "y"
    mocker.patch("stack_pr.autoland._refresh_last_landed_sha")

    ctx = LandingContext(stack=_pinned_stack([101]), plan=[ConfirmStep(condition="QA")])
    checkpointer = AutolandCheckpointer(
        path=Path("/dev/null"), branch="feat", base="main"
    )
    mocker.patch.object(checkpointer, "save")

    assert autoland.execute_plan(ctx, common_args(), _opts(), checkpointer) is True

    printed = "\n".join(
        str(c.args[0]) for c in autoland_console.print.call_args_list if c.args
    )
    assert "Step 1/1: Manual confirmation required" in printed


@pytest.mark.usefixtures("autoland_console")
def test_execute_plan_lands_a_pinned_step_that_is_still_open(mocker) -> None:  # noqa: ANN001
    mocker.patch("stack_pr.autoland._refresh_last_landed_sha")
    approval = mocker.patch("stack_pr.autoland.wait_for_approval", return_value=True)
    checks = mocker.patch("stack_pr.autoland.wait_for_checks", return_value=True)
    enqueue = mocker.patch("stack_pr.autoland.enqueue_and_wait", return_value=True)

    stack = _pinned_stack([103])
    plan = parse_plan("l 102\nl 103\n", stack, pr_is_merged=lambda pr: pr == 102)
    ctx = LandingContext(stack=stack, plan=plan)
    checkpointer = AutolandCheckpointer(
        path=Path("/dev/null"), branch="feat", base="main"
    )
    mocker.patch.object(checkpointer, "save")

    assert autoland.execute_plan(ctx, common_args(), _opts(), checkpointer) is True

    for mock in (approval, checks, enqueue):
        assert mock.call_args.args[0] is stack[0]


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
    assert cp.plan_file is None
    assert cp.branch == "feat"
    assert cp.base == "main"
    assert loaded.current_step == 1
    assert loaded.last_landed_sha == "abc"
    assert loaded.stack[0].state == autoland.PRState.MERGED
    assert [e.pr_number for e in loaded.stack] == [0, 1]
    assert [type(s) for s in loaded.plan] == [LandStep, WorkflowStep, LandStep]


def test_state_round_trip_keeps_plan_file(tmp_path) -> None:  # noqa: ANN001
    ctx = LandingContext(stack=_stack(1), plan=generate_default_plan(_stack(1)))
    sf = tmp_path / "state.json"
    plan_file = tmp_path / "plan.autoland-plan"
    AutolandCheckpointer(path=sf, branch="feat", base="main", plan_file=plan_file).save(
        ctx
    )

    cp, _ = AutolandCheckpointer.load(sf)
    assert cp.plan_file == plan_file


def test_state_round_trip_keeps_abort_reason(tmp_path) -> None:  # noqa: ANN001
    ctx = LandingContext(stack=_stack(1), plan=generate_default_plan(_stack(1)))
    ctx.aborted = True
    ctx.abort_reason = "CI failed on #0"

    sf = tmp_path / "state.json"
    AutolandCheckpointer(path=sf, branch="feat", base="main").save(ctx)

    _, loaded = AutolandCheckpointer.load(sf)
    assert loaded.aborted is True
    assert loaded.abort_reason == "CI failed on #0"


def test_load_state_version_mismatch(tmp_path) -> None:  # noqa: ANN001
    sf = tmp_path / "state.json"
    sf.write_text(
        '{"version": 999, "stack": [], "plan": [], "branch": "x", "base": "y"}'
    )
    with pytest.raises(ValueError, match="Unsupported state file version"):
        AutolandCheckpointer.load(sf)


# --- rebase + resubmit ---------------------------------------------------


@pytest.mark.usefixtures("autoland_console")
def test_rebase_and_resubmit_rededuces_base(mocker) -> None:  # noqa: ANN001
    # After rebasing onto an advanced target, the base cached at autoland start
    # is stale; resubmit must re-deduce it (else it sweeps others' commits into
    # the stack). Verify the stale base is cleared before deduce_base and that
    # command_submit receives the freshly-deduced base, not the stale one.
    stale = dataclasses.replace(common_args(), base="STALE_MERGE_BASE")
    fresh = dataclasses.replace(stale, base="FRESH_ORIGIN_MASTER")

    mocker.patch("stack_pr.autoland.run")  # git fetch / rebase
    deduce = mocker.patch("stack_pr.autoland.cli.deduce_base", return_value=fresh)
    submit = mocker.patch("stack_pr.autoland.cli.command_submit")

    autoland.rebase_and_resubmit(stale)

    # deduce_base is called with the cached base cleared...
    assert deduce.call_args.args[0].base == ""
    # ...and command_submit runs with the re-deduced base, never the stale one.
    assert submit.call_args.args[0].base == "FRESH_ORIGIN_MASTER"


@pytest.mark.usefixtures("autoland_console")
def test_rebase_and_resubmit_aborts_conflicted_rebase(
    tmp_path,  # noqa: ANN001
    monkeypatch,  # noqa: ANN001
    mocker,  # noqa: ANN001
) -> None:
    # Without --branch, autoland rebases the user's own working copy. If that
    # rebase conflicts, the failure must be reported *and* the rebase aborted,
    # rather than leaving the checkout stuck mid-rebase.
    origin = tmp_path / "origin.git"
    work = tmp_path / "work"
    git(tmp_path, "init", "--bare", "-b", "main", str(origin))
    init_repo(work)
    git(work, "remote", "add", "origin", str(origin))
    git(work, "push", "origin", "main")

    git(work, "checkout", "-b", "feature")
    (work / "file.txt").write_text("feature\n")
    git(work, "commit", "-am", "feature change")
    feature_sha = git(work, "rev-parse", "HEAD").strip()

    git(work, "checkout", "main")
    (work / "file.txt").write_text("target\n")
    git(work, "commit", "-am", "conflicting target change")
    git(work, "push", "origin", "main")
    git(work, "checkout", "feature")

    monkeypatch.chdir(work)
    submit = mocker.patch("stack_pr.autoland.cli.command_submit")

    with pytest.raises(RuntimeError, match="rebase"):
        autoland.rebase_and_resubmit(common_args())

    submit.assert_not_called()
    for state_dir in ("rebase-merge", "rebase-apply"):
        assert not (
            work / git(work, "rev-parse", "--git-path", state_dir).strip()
        ).exists()
    assert git(work, "status", "--porcelain") == ""
    assert git(work, "symbolic-ref", "--short", "HEAD").strip() == "feature"
    assert git(work, "rev-parse", "HEAD").strip() == feature_sha


@pytest.mark.usefixtures("autoland_console")
def test_run_fresh_deduces_base_inside_worktree(mocker) -> None:  # noqa: ANN001
    # With --branch, autoland lands in a temporary worktree whose HEAD is the
    # target branch. The base must be deduced *after* that worktree exists,
    # otherwise it resolves against the primary checkout's HEAD (a different
    # branch) and yields a commit that isn't an ancestor of the stack, tripping
    # the "not an ancestor of HEAD" error. Verify the ordering and that
    # discover_stack receives the freshly-deduced base.
    stale = dataclasses.replace(common_args(), base="STALE_FROM_PRIMARY_HEAD")
    fresh = dataclasses.replace(stale, base="FRESH_FROM_WORKTREE_HEAD")

    calls: list[str] = []
    mocker.patch("stack_pr.autoland.AutolandLock")

    worktree = mocker.Mock()
    worktree.create.side_effect = lambda: calls.append("worktree_create")
    mocker.patch("stack_pr.autoland.Worktree", return_value=worktree)

    def _deduce(common):  # noqa: ANN001, ANN202
        calls.append("deduce")
        return fresh

    mocker.patch("stack_pr.autoland.cli.deduce_base", side_effect=_deduce)

    seen_base: list[str] = []

    def _discover(common):  # noqa: ANN001, ANN202
        calls.append("discover")
        seen_base.append(common.base)
        return []  # empty stack -> _run_fresh exits early

    mocker.patch("stack_pr.autoland.discover_stack", side_effect=_discover)

    # dry_run keeps _run_fresh off the lock/state-file path so the test stays
    # hermetic; it still runs worktree setup -> deduce -> discover first.
    with pytest.raises(SystemExit):
        _run_fresh(stale, _opts(branch="micah/asgi", dry_run=True))

    # The worktree is created before the base is deduced, and discovery runs
    # against the freshly-deduced base rather than the stale primary-HEAD one.
    assert calls == ["worktree_create", "deduce", "discover"]
    assert seen_base == ["FRESH_FROM_WORKTREE_HEAD"]


@pytest.mark.usefixtures("autoland_console")
def test_worktree_is_removed_when_autoland_exits_before_landing(
    tmp_path,  # noqa: ANN001
    mocker,  # noqa: ANN001
    monkeypatch,  # noqa: ANN001
) -> None:
    repo = init_repo(tmp_path / "repo")
    # A branch with no commits on top of main has no stack to land.
    git(repo, "branch", "feature")
    monkeypatch.chdir(repo)
    mkdtemp = mocker.spy(autoland.tempfile, "mkdtemp")

    with pytest.raises(SystemExit) as exc:
        autoland.run_autoland(
            common_args(),
            _args(branch="feature", state_file=tmp_path / "state.json"),
            _merge_queue_cfg(),
        )

    assert exc.value.code == 1
    assert mkdtemp.call_count == 1
    assert not Path(mkdtemp.spy_return).exists()
    worktrees = git(repo, "worktree", "list", "--porcelain")
    assert worktrees.count("worktree ") == 1
    assert Path.cwd().resolve() == repo.resolve()


# --- concurrency lock ----------------------------------------------------


def test_lock_for_state_sits_next_to_state_file(tmp_path) -> None:  # noqa: ANN001
    lock = AutolandLock.for_state(tmp_path / "async.json")
    assert lock.path == tmp_path / "async.json.lock"


def test_lock_is_exclusive_and_releasable(tmp_path) -> None:  # noqa: ANN001
    path = tmp_path / "b.lock"
    first = AutolandLock(path)
    second = AutolandLock(path)

    assert first.acquire() is True
    # A second holder (distinct open file) cannot take it while the first holds.
    assert second.acquire() is False

    # Releasing frees it (and removes the file) so a later run can acquire.
    first.release()
    assert not path.exists()
    assert second.acquire() is True
    second.release()


def test_lock_is_held_reports_another_holder(tmp_path) -> None:  # noqa: ANN001
    path = tmp_path / "b.lock"
    holder = AutolandLock(path)
    probe = AutolandLock(path)

    assert probe.is_held() is False
    assert not path.exists()  # probing never creates the lock file

    assert holder.acquire() is True
    assert probe.is_held() is True
    assert probe.holder_pid() == os.getpid()
    # Probing neither steals nor breaks the holder's lock.
    assert AutolandLock(path).acquire() is False

    holder.release()
    assert probe.is_held() is False


def test_lock_release_is_idempotent(tmp_path) -> None:  # noqa: ANN001
    lock = AutolandLock(tmp_path / "b.lock")
    lock.release()  # never acquired -> no-op
    assert lock.acquire() is True
    lock.release()
    lock.release()  # double release -> no-op


@pytest.mark.parametrize(
    ("answer", "choice"),
    [("", "replan"), ("r", "replan"), ("R", "replan"), ("o", "overwrite"), ("n", None)],
)
def test_ask_replan_or_overwrite(tmp_path, autoland_console, answer, choice) -> None:  # noqa: ANN001
    autoland_console.input.return_value = answer
    assert _ask_replan_or_overwrite(tmp_path / "state.json") == choice


def test_ask_replan_or_overwrite_aborts_without_a_terminal(
    tmp_path: Path, autoland_console: Mock
) -> None:
    autoland_console.input.side_effect = EOFError
    assert _ask_replan_or_overwrite(tmp_path / "state.json") is None


# --- status report -------------------------------------------------------


@pytest.fixture
def plain_output(mocker, monkeypatch, tmp_path):  # noqa: ANN001, ANN201
    """Route output through the plain console (no wrapping) and isolate $HOME,
    where the default state directory lives."""
    mocker.patch.object(autoland, "console", _PlainConsole())
    mocker.patch.object(autoland, "HAVE_RICH", False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))


def _status(**overrides) -> None:  # noqa: ANN003
    # No [autoland] config: --status must work even without the merge queue.
    autoland.run_autoland(
        common_args(), _args(status=True, **overrides), configparser.ConfigParser()
    )


def _save_state(path: Path, *, abort_reason: str = "") -> None:
    ctx = LandingContext(stack=_stack(2), plan=generate_default_plan(_stack(2)))
    ctx.stack[0].state = autoland.PRState.MERGED
    ctx.current_step = 1
    ctx.abort_reason = abort_reason
    AutolandCheckpointer(path=path, branch="feat", base="main").save(ctx)


@pytest.mark.usefixtures("plain_output")
def test_status_without_a_run(tmp_path, capsys) -> None:  # noqa: ANN001
    sf = tmp_path / "state.json"
    _status(state_file=sf)

    out = capsys.readouterr().out
    assert "No autoland in progress" in out
    assert str(sf) in out
    assert not sf.exists()


@pytest.mark.usefixtures("plain_output")
def test_status_of_a_stopped_run(tmp_path, capsys) -> None:  # noqa: ANN001
    sf = tmp_path / "state.json"
    _save_state(sf, abort_reason="CI failed on #0")
    before = sf.read_text()

    _status(state_file=sf)

    out = capsys.readouterr().out
    assert "Stopped" in out
    assert "Branch:" in out
    assert "feat" in out
    assert str(sf) in out
    assert "1 done" in out
    assert "ABORTED: CI failed on #0" in out
    assert f"stack-pr autoland --resume --state-file {sf}" in out
    assert sf.read_text() == before  # read-only


@pytest.mark.usefixtures("plain_output")
def test_status_of_a_running_autoland(tmp_path, capsys) -> None:  # noqa: ANN001
    sf = tmp_path / "state.json"
    _save_state(sf)
    lock = AutolandLock.for_state(sf)
    assert lock.acquire()
    try:
        _status(state_file=sf)
    finally:
        lock.release()

    out = capsys.readouterr().out
    assert f"In progress (pid {os.getpid()})" in out
    assert str(lock.path) in out
    assert "--resume" not in out


@pytest.mark.usefixtures("plain_output")
def test_status_lists_saved_runs_for_other_branches(tmp_path, capsys) -> None:  # noqa: ANN001
    other = AutolandCheckpointer.default_path("feat")
    _save_state(other)

    _status(state_file=tmp_path / "state.json")

    out = capsys.readouterr().out
    assert "Other autolands with saved state" in out
    assert f"feat — stopped — {other}" in out


@pytest.mark.usefixtures("plain_output")
def test_status_json_of_a_stopped_run(tmp_path, capsys) -> None:  # noqa: ANN001
    sf = tmp_path / "state.json"
    _save_state(sf, abort_reason="CI failed on #0")

    _status(state_file=sf, output="json")

    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "stopped"
    assert report["branch"] == "feat"
    assert report["base"] == "main"
    assert report["state_file"] == str(sf)
    assert report["pid"] is None
    assert report["abort_reason"] == "CI failed on #0"
    assert report["resume_command"] == f"stack-pr autoland --resume --state-file {sf}"
    assert report["plan"]["done"] == 1
    assert report["plan"]["remaining"] == 1
    first, second = report["plan"]["steps"]
    assert first["type"] == "land"
    assert first["pr_number"] == 0
    assert first["outcome"] == "done"
    assert second["is_next"] is True


@pytest.mark.usefixtures("plain_output")
def test_status_json_of_a_running_autoland(tmp_path, capsys) -> None:  # noqa: ANN001
    sf = tmp_path / "state.json"
    _save_state(sf)
    lock = AutolandLock.for_state(sf)
    assert lock.acquire()
    try:
        _status(state_file=sf, output="json")
    finally:
        lock.release()

    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "in_progress"
    assert report["pid"] == os.getpid()
    assert report["resume_command"] is None


@pytest.mark.usefixtures("plain_output")
def test_status_json_without_a_run(tmp_path, capsys) -> None:  # noqa: ANN001
    other = AutolandCheckpointer.default_path("feat")
    _save_state(other)

    _status(state_file=tmp_path / "state.json", output="json")

    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "none"
    assert report["state_file_exists"] is False
    assert report["plan"] is None
    assert report["other_runs"] == [
        {"branch": "feat", "status": "stopped", "state_file": str(other)}
    ]


@pytest.mark.usefixtures("plain_output")
def test_status_json_reports_an_unreadable_state_file_on_stderr(
    tmp_path,  # noqa: ANN001
    capsys,  # noqa: ANN001
) -> None:
    sf = tmp_path / "state.json"
    sf.write_text("{not json")

    with pytest.raises(SystemExit):
        _status(state_file=sf, output="json")

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Failed to load state file" in captured.err


@pytest.mark.usefixtures("autoland_console")
def test_output_requires_status() -> None:
    cfg = configparser.ConfigParser()
    cfg["autoland"] = {"merge_queue": "true"}
    with pytest.raises(SystemExit):
        autoland.run_autoland(common_args(), _args(output="json"), cfg)


# --- replanning ------------------------------------------------------------

# The plan from the user's scenario: PRs 101 and 102 have landed, and the
# workflow and both confirmations after 102 finished before the run was stopped
# while landing 103.
_REPLAN_PLAN = """\
l 101
w deploy.yaml
c QA sign-off
l 102
w deploy.yaml
c metrics look healthy
c on-call agrees
l 103
"""


def _merged(*prs: int):  # noqa: ANN202
    return lambda pr: pr in prs


def _old_run() -> LandingContext:
    """The checkpoint of a run stopped while landing PR 103."""
    stack = _pinned_stack([101, 102, 103])
    plan = parse_plan(_REPLAN_PLAN, stack, pr_is_merged=_never_merged)
    stack[0].state = stack[1].state = autoland.PRState.MERGED
    stack[2].state = autoland.PRState.WAITING_FOR_CHECKS
    for step in plan[:7]:
        if isinstance(step, WorkflowStep):
            step.state = "succeeded"
        elif isinstance(step, ConfirmStep):
            step.confirmed = True
    return LandingContext(stack=stack, plan=plan, current_step=7)


def _replanned(text: str) -> tuple[list, list[int]]:
    """Parse *text* against today's stack (101 and 102 merged) and carry over."""
    stack = _pinned_stack([103, 104])
    plan = parse_plan(text, stack, pr_is_merged=_merged(101, 102))
    return plan, carry_over_progress(_old_run(), plan, stack)


def _pending(plan: list) -> list[str]:
    """The non-land steps a run of *plan* would still execute."""
    return [
        f"w {s.workflow}" if isinstance(s, WorkflowStep) else f"c {s.condition}"
        for s in plan
        if (isinstance(s, WorkflowStep) and s.state not in ("succeeded", "skipped"))
        or (isinstance(s, ConfirmStep) and not s.confirmed)
    ]


def test_replan_keeps_checkpoints_after_the_last_landed_pr() -> None:
    # Unchanged plan: a fresh run would re-wait for the workflow and re-prompt
    # both confirmations after #102; a replan carries them over.
    plan, lost = _replanned(_REPLAN_PLAN)
    assert _pending(plan) == []
    assert lost == []
    # Carried-over results are real ones, not "assumed".
    assert plan[4].state == "succeeded"


def test_replan_runs_only_steps_that_are_new() -> None:
    plan, lost = _replanned(
        _REPLAN_PLAN.replace("c on-call agrees", "c on-call agrees\nc docs updated")
        + "l 104\n"
    )
    assert _pending(plan) == ["c docs updated"]
    assert lost == []


def test_replan_keeps_credit_when_checkpoints_are_reordered() -> None:
    plan, lost = _replanned(
        _REPLAN_PLAN.replace(
            "w deploy.yaml\nc metrics look healthy\nc on-call agrees",
            "c on-call agrees\nw deploy.yaml\nc metrics look healthy",
        )
    )
    assert _pending(plan) == []
    assert lost == []


def test_replan_reruns_and_reports_a_changed_checkpoint() -> None:
    plan, lost = _replanned(
        _REPLAN_PLAN.replace("c metrics look healthy", "c p99 latency is healthy")
    )
    assert _pending(plan) == ["c p99 latency is healthy"]
    # The old step is reported by its index in the old plan.
    assert lost == [5]


def test_replan_reruns_checkpoints_now_after_a_different_set_of_prs() -> None:
    # Landing a new PR ahead of the finished checkpoints changes what they
    # vouch for, so all of them must run again.
    plan, lost = _replanned(
        "l 101\n"
        "w deploy.yaml\n"
        "c QA sign-off\n"
        "l 102\n"
        "l 103\n"
        "w deploy.yaml\n"
        "c metrics look healthy\n"
        "c on-call agrees\n"
        "l 104\n"
    )
    assert _pending(plan) == [
        "w deploy.yaml",
        "c metrics look healthy",
        "c on-call agrees",
    ]
    assert lost == [4, 5, 6]


def test_replan_matches_repeated_checkpoints_one_for_one() -> None:
    # The old run confirmed one bare 'c' after #102; a plan with two gets
    # credit for one of them only.
    old = _old_run()
    old.plan[6] = ConfirmStep(confirmed=False)
    old.plan[5] = ConfirmStep(confirmed=True)
    stack = _pinned_stack([103])
    plan = parse_plan(
        "l 101\nl 102\nc\nc\nl 103\n", stack, pr_is_merged=_merged(101, 102)
    )
    carry_over_progress(old, plan, stack)
    assert [s.confirmed for s in plan if isinstance(s, ConfirmStep)] == [True, False]


# --- replan flow -----------------------------------------------------------


def _write_checkpoint(path: Path, plan_file: Path | None) -> None:
    AutolandCheckpointer(
        path=path, branch="feat", base="main", plan_file=plan_file
    ).save(_old_run())


def _patch_replan_io(mocker, answer: str):  # noqa: ANN001, ANN202
    """Stub the git/GitHub side of a replan; return the execute_plan mock."""
    mocker.patch("stack_pr.autoland.console").input.return_value = answer
    mocker.patch("stack_pr.autoland._current_branch", return_value="feat")
    mocker.patch("stack_pr.autoland.cli.deduce_base", side_effect=lambda c: c)
    mocker.patch("stack_pr.autoland.cli.get_stack", return_value=[])
    mocker.patch("stack_pr.autoland._stack_entries", return_value=_pinned_stack([103]))
    mocker.patch("stack_pr.autoland.enrich_stack")
    mocker.patch("stack_pr.autoland._unpushed_changes", return_value=[])
    mocker.patch(
        "stack_pr.autoland.github.pr_state",
        side_effect=lambda pr: "MERGED" if pr in (101, 102) else "OPEN",
    )
    mocker.patch("stack_pr.autoland.signal.signal")
    return mocker.patch("stack_pr.autoland.execute_plan", return_value=True)


def test_replan_rereads_the_runs_plan_file_and_keeps_progress(
    tmp_path,  # noqa: ANN001
    mocker,  # noqa: ANN001
) -> None:
    plan_file = tmp_path / "plan.autoland-plan"
    plan_file.write_text(_REPLAN_PLAN.replace("c on-call agrees", "c docs updated"))
    sf = tmp_path / "state.json"
    _write_checkpoint(sf, plan_file)
    execute = _patch_replan_io(mocker, answer="y")

    _replan(common_args(), _opts(), sf)

    ctx, _common_args, _o, checkpointer = execute.call_args.args
    assert _pending(ctx.plan) == ["c docs updated"]
    # The replanned run continues in the same checkpoint, still tied to the file.
    assert checkpointer.path == sf
    assert checkpointer.plan_file == plan_file


def test_replan_declined_leaves_the_checkpoint_alone(tmp_path, mocker) -> None:  # noqa: ANN001
    sf = tmp_path / "state.json"
    _write_checkpoint(sf, None)  # no plan file: the saved plan is replayed
    before = sf.read_text()
    execute = _patch_replan_io(mocker, answer="n")

    _replan(common_args(), _opts(), sf)

    execute.assert_not_called()
    assert sf.read_text() == before


def test_replan_dry_run_previews_without_running(tmp_path, mocker) -> None:  # noqa: ANN001
    sf = tmp_path / "state.json"
    _write_checkpoint(sf, None)
    before = sf.read_text()
    execute = _patch_replan_io(mocker, answer="y")

    _replan(common_args(), _opts(dry_run=True), sf)

    execute.assert_not_called()
    assert sf.read_text() == before


@pytest.mark.usefixtures("autoland_console")
def test_replan_without_a_checkpoint_exits(tmp_path) -> None:  # noqa: ANN001
    cfg = configparser.ConfigParser()
    cfg["autoland"] = {"merge_queue": "true"}
    with pytest.raises(SystemExit):
        autoland.run_autoland(
            common_args(), _args(replan=True, state_file=tmp_path / "nope.json"), cfg
        )


# --- resume flow -------------------------------------------------------------


def _patch_resume_io(mocker, *, success: bool = True):  # noqa: ANN001, ANN202
    """Stub the git/GitHub side of a resume; return the execute_plan mock."""
    mocker.patch("stack_pr.autoland.console")
    mocker.patch("stack_pr.autoland._current_branch", return_value="feat")
    mocker.patch("stack_pr.autoland.cli.deduce_base", side_effect=lambda c: c)
    mocker.patch("stack_pr.autoland.enrich_stack")
    mocker.patch("stack_pr.autoland.signal.signal")
    return mocker.patch("stack_pr.autoland.execute_plan", return_value=success)


def _resume(sf: Path, **overrides) -> None:  # noqa: ANN003
    _run_resume(common_args(), _opts(resume=True, state_file=sf, **overrides))


def test_resume_continues_from_the_checkpointed_step(tmp_path, mocker) -> None:  # noqa: ANN001
    sf = tmp_path / "state.json"
    _save_state(sf, abort_reason="CI failed on #0")
    execute = _patch_resume_io(mocker)

    _resume(sf)

    ctx = execute.call_args.args[0]
    assert ctx.current_step == 1
    assert [e.pr_number for e in ctx.stack] == [0, 1]
    # The earlier failure no longer stands: this run gets a fresh attempt.
    assert not ctx.aborted
    assert ctx.abort_reason == ""
    # A run that finishes leaves no checkpoint behind.
    assert not sf.exists()


def test_resume_that_fails_again_keeps_the_checkpoint(tmp_path, mocker) -> None:  # noqa: ANN001
    sf = tmp_path / "state.json"
    _save_state(sf)
    _patch_resume_io(mocker, success=False)

    with pytest.raises(SystemExit) as exc:
        _resume(sf)

    assert exc.value.code == 1
    assert sf.exists()


def test_resume_of_a_finished_plan_just_cleans_up(tmp_path, mocker) -> None:  # noqa: ANN001
    sf = tmp_path / "state.json"
    ctx = LandingContext(stack=_stack(2), plan=generate_default_plan(_stack(2)))
    ctx.current_step = len(ctx.plan)
    AutolandCheckpointer(path=sf, branch="feat", base="main").save(ctx)
    execute = _patch_resume_io(mocker)

    _resume(sf)

    execute.assert_not_called()
    assert not sf.exists()


@pytest.mark.usefixtures("autoland_console")
def test_resume_without_a_checkpoint_exits(tmp_path) -> None:  # noqa: ANN001
    with pytest.raises(SystemExit):
        _resume(tmp_path / "nope.json")


def test_resume_refuses_while_another_run_holds_the_lock(tmp_path, mocker) -> None:  # noqa: ANN001
    sf = tmp_path / "state.json"
    _save_state(sf)
    execute = _patch_resume_io(mocker)
    lock = AutolandLock.for_state(sf)
    assert lock.acquire()
    try:
        with pytest.raises(SystemExit):
            _resume(sf)
    finally:
        lock.release()

    execute.assert_not_called()
    assert sf.exists()


def test_resume_refuses_a_different_branch(tmp_path, mocker) -> None:  # noqa: ANN001
    sf = tmp_path / "state.json"
    _save_state(sf)  # saved for branch "feat"
    execute = _patch_resume_io(mocker)

    with pytest.raises(SystemExit):
        _resume(sf, branch="other")

    execute.assert_not_called()
    assert sf.exists()


# --- taking over a running autoland -----------------------------------------

_HOLDER = """
import signal, time
from pathlib import Path
from stack_pr.autoland import AutolandLock
# A shell runs background jobs with SIGINT ignored, and Python keeps an
# inherited SIG_IGN; restore Ctrl+C so the takeover's SIGINT stops this holder.
signal.signal(signal.SIGINT, signal.default_int_handler)
lock = AutolandLock(Path({path!r}))
assert lock.acquire()
print("locked", flush=True)
try:
    time.sleep(60)
finally:
    lock.release()
"""


def _spawn_holder(path: Path) -> subprocess.Popen:
    proc = subprocess.Popen(
        [sys.executable, "-c", _HOLDER.format(path=str(path))],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    assert proc.stdout is not None
    assert proc.stdout.readline().strip() == "locked"
    return proc


def test_stop_running_autoland_takes_over_the_lock(tmp_path, autoland_console) -> None:  # noqa: ANN001
    autoland_console.input.return_value = "y"
    path = tmp_path / "state.json.lock"
    holder = _spawn_holder(path)
    lock = AutolandLock(path)
    try:
        assert _stop_running_autoland(lock) is True
        # The other run was interrupted, and this process now holds the lock.
        assert holder.wait(timeout=10) != 0
        assert AutolandLock(path).acquire() is False
    finally:
        lock.release()
        holder.kill()


def test_stop_running_autoland_declined_leaves_it_running(
    tmp_path: Path, autoland_console: Mock
) -> None:
    autoland_console.input.return_value = "n"
    path = tmp_path / "state.json.lock"
    holder = _spawn_holder(path)
    try:
        assert _stop_running_autoland(AutolandLock(path)) is False
        assert holder.poll() is None
        assert AutolandLock(path).is_held()
    finally:
        holder.kill()
        holder.wait()


# --- approval ------------------------------------------------------------


@pytest.mark.parametrize(
    ("decision", "approved"),
    [
        ("APPROVED", True),
        ("", True),  # the branch requires no review
        ("REVIEW_REQUIRED", False),
        ("CHANGES_REQUESTED", False),
    ],
)
@pytest.mark.usefixtures("autoland_console")
def test_wait_for_approval_by_review_decision(
    mocker,  # noqa: ANN001
    decision: str,
    approved: bool,
) -> None:
    mocker.patch.object(autoland.github, "pr_state", return_value="OPEN")
    mocker.patch.object(autoland.github, "review_decision", return_value=decision)
    ctx = LandingContext()
    # Abort the wait on its first poll, so a PR that isn't approved returns.
    mocker.patch(
        "stack_pr.autoland.resilient_sleep",
        side_effect=lambda _s: setattr(ctx, "aborted", True),
    )

    entry = _pinned_stack([101])[0]
    assert autoland.wait_for_approval(entry, opts=_opts(), ctx=ctx) is approved


# --- merging a run of land steps as a GitHub stack -------------------------


def test_merge_as_stack_defaults_on_and_flag_overrides_config() -> None:
    assert _parsed_opts().merge_as_stack is True

    cfg = configparser.ConfigParser()
    cfg.add_section("autoland")
    cfg.set("autoland", "merge_as_stack", "false")
    assert AutolandOptions.from_config_and_args(cfg, _args()).merge_as_stack is False
    flagged = AutolandOptions.from_config_and_args(cfg, _args(merge_as_stack=True))
    assert flagged.merge_as_stack is True


def test_native_stack_merge_runs_are_split_by_checkpoints() -> None:
    stack = _pinned_stack([101, 102, 103, 104, 105])
    plan = parse_plan("l\nl\nw deploy.yaml\nl\nc QA\nl\nl\n", stack)
    ctx = LandingContext(stack=stack, plan=plan)

    runs = autoland.native_stack_runs(ctx)

    # A lone 'l' between two checkpoints has nothing to merge alongside.
    assert [(first, [e.pr_number for e in es]) for first, es in runs] == [
        (0, [101, 102]),
        (5, [104, 105]),
    ]


def test_native_stack_merge_run_starts_above_merged_prs() -> None:
    stack = _pinned_stack([101, 102, 103])
    ctx = LandingContext(stack=stack, plan=parse_plan("l\nl\nl\n", stack))
    stack[0].state = autoland.PRState.MERGED

    assert autoland.native_stack_run(ctx, 0) == []
    assert [e.pr_number for e in autoland.native_stack_run(ctx, 1)] == [102, 103]


class _FakeNativeStackGitHub:
    """The slice of GitHub a stack merge talks to, holding the stacks in memory."""

    def __init__(self) -> None:
        self.stacks: dict[int, list[int]] = {}
        self.merged: set[int] = set()
        self.created: list[list[int]] = []
        self.unstacked: list[int] = []
        self.merge_requests: list[int] = []
        # (PR, new base) for each base change, and the open PRs' bases at the
        # time of each merge request.
        self.base_changes: list[tuple[int, str]] = []
        self.bases_at_merge: list[list[tuple[int, str]]] = []
        self.create_error = ""
        self.merge_error = ""
        self.set_base_error = ""
        # The PRs a merge request lands (default: the whole stack up to the PR
        # it was made on), and the status GitHub then reports for it.
        self.lands: list[int] | None = None
        self.status = "merged"

    def find_native_stack(self, pr: int) -> dict | None:
        for number, prs in self.stacks.items():
            if pr in prs:
                return {
                    "number": number,
                    "pull_requests": [
                        {"number": n, "state": "closed" if n in self.merged else "open"}
                        for n in prs
                    ],
                }
        return None

    def create_native_stack(self, prs: list[int]) -> dict:
        if self.create_error:
            raise RuntimeError(self.create_error)
        self.created.append(prs)
        self.stacks[7] = prs
        return {"number": 7}

    def unstack_native_stack(self, number: int) -> None:
        self.unstacked.append(number)
        self.stacks.pop(number)

    def set_base(self, pr: int, base: str) -> None:
        if self.set_base_error:
            raise RuntimeError(self.set_base_error)
        self.base_changes.append((pr, base))

    def merge_async(self, pr: int, *, merge_queue: bool) -> str:
        self.merge_requests.append(pr)
        self.bases_at_merge.append(list(self.base_changes))
        if self.merge_error:
            raise RuntimeError(self.merge_error)
        prs = next(p for p in self.stacks.values() if pr in p)
        self.merged.update(self.lands if self.lands is not None else prs)
        return "uuid-1"

    def merge_async_status(self, _pr: int, _uuid: str) -> tuple[str, str]:
        return self.status, "conflict in PR #102" if self.status == "failed" else ""

    def pr_state(self, pr: int) -> str:
        return "MERGED" if pr in self.merged else "OPEN"

    def in_merge_queue(self, _pr: int) -> bool:
        return True

    def has_merge_queue(self, _branch: str) -> bool:
        return True


def _land_with_fake_github(mocker, plan_text: str, prs: list[int], **opts):  # noqa: ANN001, ANN003, ANN202
    """Set up running *plan_text* over a stack of *prs* against a fake GitHub.

    Approval, checks and mergeability are taken as given, and a PR landed one
    at a time merges straight away. Returns the fake, a function that runs the
    plan, the rebase mock, and a function listing the PRs landed one at a time.
    """
    fake = _FakeNativeStackGitHub()
    mocker.patch.object(autoland, "github", fake)
    mocker.patch("stack_pr.autoland.console")
    mocker.patch("stack_pr.autoland.resilient_sleep")
    mocker.patch("stack_pr.autoland._refresh_last_landed_sha")
    mocker.patch("stack_pr.autoland.wait_for_approval", return_value=True)
    mocker.patch("stack_pr.autoland.wait_for_checks", return_value=True)
    mocker.patch(
        "stack_pr.autoland.wait_for_mergeable",
        return_value=autoland.MergeableResult(ready=True),
    )
    rebase = mocker.patch("stack_pr.autoland.rebase_and_resubmit")

    def land_one(entry, **_kw) -> bool:  # noqa: ANN001, ANN003
        fake.merged.add(entry.pr_number)
        entry.state = autoland.PRState.MERGED
        return True

    one_at_a_time = mocker.patch(
        "stack_pr.autoland.enqueue_and_wait", side_effect=land_one
    )

    stack = _pinned_stack(prs)
    ctx = LandingContext(stack=stack, plan=parse_plan(plan_text, stack))
    checkpointer = AutolandCheckpointer(
        path=Path("/dev/null"), branch="feat", base="main"
    )
    mocker.patch.object(checkpointer, "save")

    def execute() -> bool:
        return autoland.execute_plan(ctx, common_args(), _opts(**opts), checkpointer)

    def landed_one_by_one() -> list[int]:
        return [c.args[0].pr_number for c in one_at_a_time.call_args_list]

    return fake, execute, rebase, landed_one_by_one


def test_consecutive_land_steps_merge_as_one_stack(mocker) -> None:  # noqa: ANN001
    fake, execute, rebase, landed_one_by_one = _land_with_fake_github(
        mocker, "l\nl\nl\n", [101, 102, 103, 104]
    )

    assert execute() is True

    assert fake.created == [[101, 102, 103]]
    assert fake.merge_requests == [103]  # one request, on the top of the run
    assert fake.merged == {101, 102, 103}
    assert landed_one_by_one() == []
    # #104 stays open, so the stack is rebased onto the landed code — once.
    rebase.assert_called_once()


def test_stack_merge_first_bases_the_pr_above_the_run_on_the_target(mocker) -> None:  # noqa: ANN001
    # Otherwise GitHub closes #104 once the run's branches are deleted.
    fake, execute, _rebase, _landed = _land_with_fake_github(
        mocker, "l\nl\nl\n", [101, 102, 103, 104]
    )

    assert execute() is True

    assert fake.bases_at_merge == [[(104, "main")]]


def test_stack_merge_falls_back_when_the_pr_above_cannot_be_rebased(mocker) -> None:  # noqa: ANN001
    fake, execute, _rebase, landed_one_by_one = _land_with_fake_github(
        mocker, "l\nl\n", [101, 102, 103]
    )
    fake.set_base_error = "HTTP 502"

    assert execute() is True

    assert fake.merge_requests == []
    assert fake.unstacked == [7]
    assert landed_one_by_one() == [101, 102]


def test_merge_as_stack_off_lands_one_at_a_time(mocker) -> None:  # noqa: ANN001
    fake, execute, _rebase, landed_one_by_one = _land_with_fake_github(
        mocker, "l\nl\n", [101, 102], merge_as_stack=False
    )

    assert execute() is True

    assert fake.created == []
    assert landed_one_by_one() == [101, 102]


def test_native_stack_merge_falls_back_when_the_stack_cannot_be_created(mocker) -> None:  # noqa: ANN001
    fake, execute, _rebase, landed_one_by_one = _land_with_fake_github(
        mocker, "l\nl\nl\n", [101, 102, 103]
    )
    fake.create_error = "HTTP 422: base ref does not match"

    assert execute() is True

    assert fake.merge_requests == []
    assert landed_one_by_one() == [101, 102, 103]


def test_partly_failed_stack_merge_lands_the_rest_one_at_a_time(mocker) -> None:  # noqa: ANN001
    fake, execute, rebase, landed_one_by_one = _land_with_fake_github(
        mocker, "l\nl\nl\n", [101, 102, 103]
    )
    fake.lands = [101]
    fake.status = "failed"

    assert execute() is True

    # The stack is dissolved so the rest can merge outside of it, and is not
    # retried as a stack; #101 stays landed and the rest is rebased onto it.
    assert fake.unstacked == [7]
    assert fake.merge_requests == [103]
    assert landed_one_by_one() == [102, 103]
    assert rebase.call_count == 2  # after the stack merge, and after #102


def test_native_stack_merge_waits_for_a_request_already_in_flight(mocker) -> None:  # noqa: ANN001
    fake, execute, _rebase, landed_one_by_one = _land_with_fake_github(
        mocker, "l\nl\n", [101, 102]
    )
    fake.merge_error = "gh: existing merge request already enqueued (HTTP 409)"
    # The earlier request merges the PRs while we wait for it.
    states = iter(["OPEN", "OPEN", "MERGED", "MERGED"])
    mocker.patch.object(fake, "pr_state", side_effect=lambda _pr: next(states))

    assert execute() is True

    assert landed_one_by_one() == []
    assert fake.unstacked == []


def test_stack_merge_keeps_waiting_when_the_queue_lookup_fails(mocker) -> None:  # noqa: ANN001
    fake, execute, _rebase, landed_one_by_one = _land_with_fake_github(
        mocker, "l\nl\n", [101, 102]
    )
    fake.lands = []  # queued, not merged yet
    fake.status = "enqueued"

    def lookup_fails(_pr: int) -> None:
        # The lookup fails while the stack is still queued; it merges meanwhile.
        fake.merged.update({101, 102})

    mocker.patch.object(fake, "in_merge_queue", side_effect=lookup_fails)

    assert execute() is True

    assert fake.merge_requests == [102]
    assert fake.unstacked == []
    assert landed_one_by_one() == []


def test_native_stack_merge_reuses_a_stack_the_run_is_at_the_bottom_of(mocker) -> None:  # noqa: ANN001
    fake, execute, _rebase, _landed = _land_with_fake_github(
        mocker, "l\nl\n", [101, 102, 103]
    )
    fake.stacks[3] = [101, 102, 103]
    fake.lands = [101, 102]

    assert execute() is True

    assert fake.created == []
    assert fake.merge_requests == [102]


def test_native_stack_merge_leaves_a_mismatched_stack_alone(mocker) -> None:  # noqa: ANN001
    fake, execute, _rebase, landed_one_by_one = _land_with_fake_github(
        mocker, "l\nl\n", [101, 102]
    )
    fake.stacks[3] = [100, 101, 102]  # #100 isn't part of this plan

    assert execute() is True

    assert fake.created == []
    assert fake.unstacked == []
    assert landed_one_by_one() == [101, 102]


# ---------------------------------------------------------------------------
# Retrying failed commands
# ---------------------------------------------------------------------------


def _fake_subprocess(mocker, *, returncode: int = 1, stderr: str = "", exc=None):  # noqa: ANN001, ANN202
    """Patch the subprocess boundary of ``run``; returns the mock to count calls."""
    mocker.patch.object(autoland, "_RETRY_DELAY", 0)

    def _run(cmd, **_kwargs):  # noqa: ANN001, ANN003, ANN202
        if exc is not None:
            raise exc
        return subprocess.CompletedProcess(cmd, returncode, stdout="", stderr=stderr)

    return mocker.patch.object(autoland.subprocess, "run", side_effect=_run)


def test_enqueue_is_not_resent_after_a_transient_looking_failure(mocker) -> None:  # noqa: ANN001
    # The merge may have gone through server-side even though the response
    # was lost, so it must not be sent again.
    fake = _fake_subprocess(mocker, stderr="unexpected EOF")
    with pytest.raises(RuntimeError):
        autoland.github.enqueue(12)
    assert fake.call_count == 1


def test_merge_async_is_not_resent_after_a_transient_looking_failure(mocker) -> None:  # noqa: ANN001
    mocker.patch.object(autoland.github, "_owner_repo", ("o", "r"))
    fake = _fake_subprocess(mocker, stderr="HTTP 502: Bad Gateway")
    with pytest.raises(RuntimeError):
        autoland.github.merge_async(12, merge_queue=True)
    assert fake.call_count == 1


def test_read_is_retried_after_a_transient_failure(mocker) -> None:  # noqa: ANN001
    fake = _fake_subprocess(mocker, stderr="HTTP 502: Bad Gateway")
    with pytest.raises(RuntimeError):
        autoland.github.pr_state(12)
    assert fake.call_count > 1


def test_missing_executable_is_not_retried(mocker) -> None:  # noqa: ANN001
    fake = _fake_subprocess(mocker, exc=FileNotFoundError("gh"))
    with pytest.raises(RuntimeError):
        autoland.run(["gh", "pr", "view", "12"], quiet=True)
    assert fake.call_count == 1
