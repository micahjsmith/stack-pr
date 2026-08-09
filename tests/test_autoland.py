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
    _describe_step,
    _next_steps_lines,
    _run_fresh,
    evaluate_checks,
    format_plan_for_editor,
    generate_default_plan,
    parse_plan,
    plan_from_file,
)
from stack_pr.cli import CommonArgs


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


def _opts(**overrides) -> AutolandOptions:  # noqa: ANN003
    return AutolandOptions.from_config_and_args(
        configparser.ConfigParser(), _args(**overrides)
    )


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
    assert autoland.wait_for_workflow(step, opts=_opts(), common=_common(), ctx=ctx)
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
    assert not autoland.wait_for_workflow(step, opts=_opts(), common=_common(), ctx=ctx)


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


def test_editor_format_pins_pr_numbers() -> None:
    stack = _pinned_stack([101, 102])
    text = format_plan_for_editor(stack, generate_default_plan(stack))
    assert "l 101    # PR 101" in text
    assert "l 102    # PR 102" in text


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


def test_plan_from_file_missing_file_exits(tmp_path, mocker) -> None:  # noqa: ANN001
    mocker.patch("stack_pr.autoland.console")
    with pytest.raises(SystemExit) as exc:
        plan_from_file(tmp_path / "nope.txt", _stack(1))
    assert exc.value.code == 1


def test_plan_from_file_invalid_content_exits(tmp_path, mocker) -> None:  # noqa: ANN001
    mocker.patch("stack_pr.autoland.console")
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


def test_run_autoland_rejects_plan_file_with_count(mocker) -> None:  # noqa: ANN001
    mocker.patch("stack_pr.autoland.console")
    with pytest.raises(SystemExit) as exc:
        autoland.run_autoland(
            _common(), _args(plan_file="plan.txt", count=2), _merge_queue_cfg()
        )
    assert exc.value.code == 1


def test_run_autoland_rejects_plan_file_with_resume(mocker) -> None:  # noqa: ANN001
    mocker.patch("stack_pr.autoland.console")
    with pytest.raises(SystemExit) as exc:
        autoland.run_autoland(
            _common(), _args(plan_file="plan.txt", resume=True), _merge_queue_cfg()
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


def test_render_status_plain_shows_already_landed_step() -> None:
    ctx = LandingContext(
        stack=_pinned_stack([102]),
        plan=[LandStep(entry_index=-1, pr_number=101), LandStep(entry_index=0)],
    )
    assert "Land PR #101 — already landed" in autoland.render_status_plain(ctx)


@pytest.mark.skipif(not autoland.HAVE_RICH, reason="requires the rich extra")
def test_render_status_table_handles_already_landed_step() -> None:
    # The already-landed step has no stack entry behind it; rendering it must
    # not reach into ctx.stack.
    ctx = LandingContext(
        stack=_pinned_stack([102]),
        plan=[LandStep(entry_index=-1, pr_number=101), LandStep(entry_index=0)],
    )
    rendered = autoland.render_status_table(ctx)
    from rich.console import Console as RichConsole  # noqa: PLC0415

    with RichConsole(record=True, width=100) as rich_console:
        rich_console.print(rendered)
    text = rich_console.export_text()
    assert "Land PR #101" in text
    assert "Already landed" in text


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


def test_execute_plan_skips_landed_prefix_and_targets_last_landed_sha(mocker) -> None:  # noqa: ANN001
    mocker.patch("stack_pr.autoland.console")
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

    assert autoland.execute_plan(ctx, _common(), _opts(), checkpointer) is True

    # Only the trailing workflow runs; the skipped one and the confirmation
    # (which would otherwise block on stdin) are passed over.
    wait_for_workflow.assert_called_once()
    # The trailing workflow waits for #102's code: only the last PR of the
    # landed prefix is looked up, not every PR in it.
    pinned = [c.args[2] for c in refresh.call_args_list if len(c.args) > 2]
    assert pinned == [102]


def test_confirm_step_banner_is_a_single_line(mocker) -> None:  # noqa: ANN001
    # Regression: the banner was once built as two list elements, so the join
    # split "Step 1/1: Manual confirmation required" across two lines.
    console = mocker.patch("stack_pr.autoland.console")
    console.input.return_value = "y"
    mocker.patch("stack_pr.autoland._refresh_last_landed_sha")

    ctx = LandingContext(stack=_pinned_stack([101]), plan=[ConfirmStep(condition="QA")])
    checkpointer = AutolandCheckpointer(
        path=Path("/dev/null"), branch="feat", base="main"
    )
    mocker.patch.object(checkpointer, "save")

    assert autoland.execute_plan(ctx, _common(), _opts(), checkpointer) is True

    printed = "\n".join(str(c.args[0]) for c in console.print.call_args_list if c.args)
    assert "Step 1/1: Manual confirmation required" in printed


def test_execute_plan_lands_a_pinned_step_that_is_still_open(mocker) -> None:  # noqa: ANN001
    mocker.patch("stack_pr.autoland.console")
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

    assert autoland.execute_plan(ctx, _common(), _opts(), checkpointer) is True

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
    assert cp.branch == "feat"
    assert cp.base == "main"
    assert loaded.current_step == 1
    assert loaded.last_landed_sha == "abc"
    assert loaded.stack[0].state == autoland.PRState.MERGED
    assert [e.pr_number for e in loaded.stack] == [0, 1]
    assert [type(s) for s in loaded.plan] == [LandStep, WorkflowStep, LandStep]


def test_load_state_version_mismatch(tmp_path) -> None:  # noqa: ANN001
    sf = tmp_path / "state.json"
    sf.write_text(
        '{"version": 999, "stack": [], "plan": [], "branch": "x", "base": "y"}'
    )
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


def test_run_fresh_deduces_base_inside_worktree(mocker) -> None:  # noqa: ANN001
    # With --branch, autoland lands in a temporary worktree whose HEAD is the
    # target branch. The base must be deduced *after* that worktree exists,
    # otherwise it resolves against the primary checkout's HEAD (a different
    # branch) and yields a commit that isn't an ancestor of the stack, tripping
    # the "not an ancestor of HEAD" error. Verify the ordering and that
    # discover_stack receives the freshly-deduced base.
    stale = dataclasses.replace(_common(), base="STALE_FROM_PRIMARY_HEAD")
    fresh = dataclasses.replace(stale, base="FRESH_FROM_WORKTREE_HEAD")

    calls: list[str] = []

    mocker.patch("stack_pr.autoland.console")
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
