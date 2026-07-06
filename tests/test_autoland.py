import argparse
import configparser
import dataclasses
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).parent.parent / "src"))

import pytest

from stack_pr import autoland
from stack_pr.autoland import (
    AutolandCheckpointer,
    AutolandLock,
    AutolandOptions,
    CheckStatus,
    ConfirmStep,
    LandingContext,
    LandStep,
    StackEntry,
    WorkflowStep,
    _confirm_overwrite_state,
    evaluate_checks,
    generate_default_plan,
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


# --- workflow checkpoint SHA ---------------------------------------------


def _opts(**overrides) -> AutolandOptions:  # noqa: ANN003
    base = {
        "merge_queue": True,
        "required_checks": [],
        "poll_interval": 0,
        "max_check_retries": 0,
        "max_queue_retries": 0,
        "merge_timeout": 0,
        "workflow_timeout": 3600,
        "default_workflow": None,
        "dry_run": False,
        "branch": None,
        "interactive": False,
        "resume": False,
        "state_file": None,
        "always_cleanup": False,
    }
    base.update(overrides)
    return AutolandOptions(**base)


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
    autoland._refresh_last_landed_sha(ctx, _common(), pr_number=42)  # noqa: SLF001
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
    autoland._refresh_last_landed_sha(ctx, _common(), pr_number=42)  # noqa: SLF001
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
    assert autoland.wait_for_workflow(
        step, opts=_opts(), common=_common(), ctx=ctx
    )
    assert step.state == "succeeded"


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
        step, opts=_opts(), common=_common(), ctx=ctx
    )


# --- plan parsing --------------------------------------------------------


def _stack(n: int) -> list:
    return [
        StackEntry(pr_url=f"u/{i}", pr_number=i, branch=f"b{i}") for i in range(n)
    ]


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


# --- rebase + resubmit ---------------------------------------------------


def test_rebase_and_resubmit_rededuces_base(mocker) -> None:  # noqa: ANN001
    # After rebasing onto an advanced target, the base cached at autoland start
    # is stale; resubmit must re-deduce it (else it sweeps others' commits into
    # the stack). Verify the stale base is cleared before deduce_base and that
    # command_submit receives the freshly-deduced base, not the stale one.
    stale = dataclasses.replace(_common(), base="STALE_MERGE_BASE")
    fresh = dataclasses.replace(stale, base="FRESH_ORIGIN_MASTER")

    mocker.patch("stack_pr.autoland.run")  # git fetch / rebase
    mocker.patch("stack_pr.autoland.console")
    deduce = mocker.patch("stack_pr.autoland.cli.deduce_base", return_value=fresh)
    submit = mocker.patch("stack_pr.autoland.cli.command_submit")

    autoland.rebase_and_resubmit(stale)

    # deduce_base is called with the cached base cleared...
    assert deduce.call_args.args[0].base == ""
    # ...and command_submit runs with the re-deduced base, never the stale one.
    assert submit.call_args.args[0].base == "FRESH_ORIGIN_MASTER"


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


def test_lock_release_is_idempotent(tmp_path) -> None:  # noqa: ANN001
    lock = AutolandLock(tmp_path / "b.lock")
    lock.release()  # never acquired -> no-op
    assert lock.acquire() is True
    lock.release()
    lock.release()  # double release -> no-op


def test_confirm_overwrite_state(tmp_path, mocker) -> None:  # noqa: ANN001
    console = mocker.patch("stack_pr.autoland.console")
    sf = tmp_path / "state.json"

    console.input.return_value = "y"
    assert _confirm_overwrite_state(sf) is True

    console.input.return_value = "n"
    assert _confirm_overwrite_state(sf) is False

    # Non-interactive (EOF) must not overwrite.
    console.input.side_effect = EOFError
    assert _confirm_overwrite_state(sf) is False
