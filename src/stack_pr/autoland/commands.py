"""The ``autoland`` subcommand: its parser, and the fresh/resume/replan flows."""

from __future__ import annotations

import argparse
import configparser
import json
import os
import signal
import sys
import time
from pathlib import Path

from stack_pr import cli, git
from stack_pr.autoland import discovery, engine, runtime
from stack_pr.autoland.display import _describe_step, _escape_markup, print_status
from stack_pr.autoland.model import LandingContext, LandStep, PlanStep, StackEntry
from stack_pr.autoland.native_stack import print_native_stack_runs
from stack_pr.autoland.options import AutolandOptions
from stack_pr.autoland.plan import (
    PLAN_SUFFIX,
    _checkpoint_keys,
    carry_over_progress,
    edit_plan_interactive,
    format_plan_for_editor,
    generate_default_plan,
    parse_plan,
    plan_from_file,
)
from stack_pr.autoland.state import AutolandCheckpointer, AutolandLock, _state_path
from stack_pr.autoland.status import show_status
from stack_pr.autoland.worktree import Worktree

# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------


def register_parser(
    subparsers: argparse._SubParsersAction, common_parser: argparse.ArgumentParser
) -> None:
    """Register the `autoland` subparser. Called from cli.create_argparser."""
    p = subparsers.add_parser(
        "autoland",
        help="Land the whole stack through the GitHub merge queue",
        parents=[common_parser],
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Discover and display the stack, then exit.",
    )
    p.add_argument(
        "--max-check-retries",
        type=int,
        default=None,
        help="Max times to rerun failed CI checks (config: autoland.max_check_retries).",
    )
    p.add_argument(
        "--max-queue-retries",
        type=int,
        default=None,
        help="Max retries after a merge-queue boot (config: autoland.max_queue_retries).",
    )
    p.add_argument(
        "--poll-interval",
        type=int,
        default=None,
        help="Seconds between status polls (config: autoland.poll_interval).",
    )
    p.add_argument(
        "--workflow-timeout",
        type=int,
        default=None,
        help=(
            "Seconds to wait for a workflow checkpoint "
            "(config: autoland.workflow_timeout)."
        ),
    )
    p.add_argument(
        "-n",
        "--count",
        type=int,
        default=None,
        metavar="N",
        help="Land only the bottom N PRs of the stack (default: the whole stack).",
    )
    p.add_argument(
        "--branch",
        default=None,
        metavar="BRANCH",
        help="Land a stack rooted on BRANCH using a temporary worktree.",
    )
    p.add_argument(
        "--merge-as-stack",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=(
            "Merge each run of consecutive 'l' steps in one go, as a GitHub "
            "stack, instead of one PR at a time (config: autoland.merge_as_stack; "
            "default: on)."
        ),
    )
    p.add_argument(
        "--always-cleanup",
        action="store_true",
        help="Always remove the temporary worktree, even on failure.",
    )
    # The plan comes from one source: the default, the interactive editor, or a
    # file. -i and --plan-file are therefore mutually exclusive.
    plan_source = p.add_mutually_exclusive_group()
    plan_source.add_argument(
        "-i",
        "--interactive",
        action="store_true",
        help="Edit the landing plan in $EDITOR (add workflow/confirm checkpoints).",
    )
    plan_source.add_argument(
        "--plan-file",
        type=Path,
        default=None,
        metavar="PATH",
        help=(
            "Load the landing plan from a file (same format as the -i editor: "
            "'l' / 'w <workflow>' / 'c [condition]' lines). Plans conventionally "
            f"use the {PLAN_SUFFIX} suffix."
        ),
    )
    run_mode = p.add_mutually_exclusive_group()
    run_mode.add_argument(
        "--resume",
        action="store_true",
        help="Resume a previously interrupted run from its checkpoint.",
    )
    run_mode.add_argument(
        "--replan",
        action="store_true",
        help=(
            "Replace the plan of an interrupted or running autoland and continue, "
            "keeping its progress: landed PRs, finished workflows, and given "
            "confirmations carry over. Re-reads the run's plan file, or the one "
            "given with --plan-file, or opens the current plan with -i. Stops a "
            "running autoland first (after asking)."
        ),
    )
    run_mode.add_argument(
        "--status",
        action="store_true",
        help=(
            "Show whether an autoland is in progress for the branch, where its "
            "state file is, and its progress as of the last checkpoint. Changes "
            "nothing."
        ),
    )
    p.add_argument(
        "-o",
        "--output",
        choices=["text", "json"],
        default="text",
        help="Output format for --status (default: text).",
    )
    p.add_argument(
        "--state-file",
        type=Path,
        default=None,
        metavar="PATH",
        help="Override the state file path (default: ~/.stack-pr/autoland/<branch>.json).",
    )


def run_autoland(
    common: cli.CommonArgs,
    args: argparse.Namespace,
    config: configparser.ConfigParser,
) -> None:
    """Entry point for `stack-pr autoland`."""
    opts = AutolandOptions.from_config_and_args(config, args)

    # Purely informational, so it works whatever the repo's landing strategy.
    output = getattr(args, "output", "text")
    if getattr(args, "status", False):
        show_status(opts, output=output)
        return
    if output != "text":
        runtime.console.print("[red]-o/--output only applies to --status.[/red]")
        sys.exit(1)

    # Merge-queue is the only supported strategy for now. Fail early otherwise.
    if not opts.merge_queue:
        raise NotImplementedError(
            "stack-pr autoland currently supports only repositories that use "
            "the GitHub merge queue. Enable it with:\n"
            "    stack-pr config autoland.merge_queue=true"
        )

    # A file plan replaces the whole plan, so --count (which only shapes the
    # generated default) and --resume (which restores the plan from a
    # checkpoint) have nothing to act on.
    if opts.plan_file is not None and opts.count is not None:
        runtime.console.print(
            "[red]--plan-file and --count can't be combined: the file already "
            "specifies which PRs to land.[/red]"
        )
        sys.exit(1)
    if opts.plan_file is not None and opts.resume:
        runtime.console.print(
            "[red]--plan-file and --resume can't be combined: a resumed run "
            "restores its plan from the checkpoint. To continue with a new plan, "
            "use --replan --plan-file.[/red]"
        )
        sys.exit(1)
    if opts.replan and opts.count is not None:
        runtime.console.print(
            "[red]--replan and --count can't be combined: the plan being "
            "replaced already specifies which PRs to land.[/red]"
        )
        sys.exit(1)

    if opts.replan:
        _run_replan(common, opts)
        return
    if opts.resume:
        _run_resume(common, opts)
        return
    _run_fresh(common, opts)


def _dispose_worktree(
    worktree: Worktree | None, opts: AutolandOptions, *, succeeded: bool
) -> None:
    """Remove the worktree, or preserve it after a failure for debugging."""
    if worktree is None:
        return
    if succeeded or opts.always_cleanup:
        worktree.remove()
    else:
        worktree.announce_preserved()


def _install_signal_handler(
    ctx: LandingContext,
    checkpointer: AutolandCheckpointer,
    worktree: Worktree | None,
    opts: AutolandOptions,
) -> None:
    # Installed as landing starts: from here on, the handler and _finish
    # decide whether the worktree is kept.
    if worktree is not None:
        worktree.landing = True

    def handler(_sig: int, _frame: object) -> None:
        ctx.aborted = True
        ctx.abort_reason = "User interrupted (Ctrl+C)"
        checkpointer.save(ctx)
        runtime.console.print(
            "\n[red bold]Interrupted! State saved. Resume with --resume.[/red bold]\n"
        )
        print_status(ctx)
        _dispose_worktree(worktree, opts, succeeded=False)
        sys.exit(130)

    signal.signal(signal.SIGINT, handler)


def _finish(
    ctx: LandingContext,
    checkpointer: AutolandCheckpointer,
    worktree: Worktree | None,
    opts: AutolandOptions,
    *,
    success: bool,
) -> None:
    runtime.console.print("\n")
    print_status(ctx)
    if success:
        # Count only the steps this run actually landed: a step whose PR landed
        # in an earlier run has no entry in today's stack to count against.
        landed = sum(
            1 for s in ctx.plan if isinstance(s, LandStep) and not s.already_landed
        )
        total = len(ctx.stack)
        if landed < total:
            runtime.console.print(
                f"\n[bold green]Landed {landed} of {total} PRs; "
                f"{total - landed} still open in the stack.[/bold green]\n"
            )
        else:
            runtime.console.print(
                "\n[bold green]All PRs landed successfully![/bold green]\n"
            )
        checkpointer.delete()
    else:
        runtime.console.print(
            f"\n[bold red]Landing failed: {ctx.abort_reason}[/bold red]\n"
        )
        runtime.console.print(
            f"[dim]State saved to {checkpointer.path} — resume with --resume[/dim]\n"
        )
    _dispose_worktree(worktree, opts, succeeded=success)
    if not success:
        sys.exit(1)


def _ask_replan_or_overwrite(state_path: Path) -> str | None:
    """Ask what to do about an existing checkpoint when starting a new run.

    Returns "replan", "overwrite", or None to abort. Replanning is the default:
    it is the safe choice, and it previews the result before running anything.
    """
    runtime.console.print(
        "\n[bold yellow]An autoland is already in progress for this "
        "branch.[/bold yellow]\n"
        f"[yellow]A checkpoint from that run exists at {state_path}.[/yellow]\n\n"
        "  [bold]r[/bold]  replan: keep its progress (landed PRs, finished "
        "workflows, given confirmations) and continue with this plan\n"
        "  [bold]o[/bold]  overwrite: discard that progress and start over\n"
    )
    try:
        answer = (
            runtime.console.input(
                "[yellow]Choose r or o (Enter = r; anything else aborts): [/yellow]"
            )
            .strip()
            .lower()
        )
    except EOFError:
        return None
    if answer in ("", "r"):
        return "replan"
    if answer == "o":
        return "overwrite"
    return None


def _run_fresh(common: cli.CommonArgs, opts: AutolandOptions) -> None:
    branch = opts.branch or git.get_current_branch_name()
    state_path = opts.state_file or AutolandCheckpointer.default_path(branch)

    # A dry run only previews the plan; it neither writes state nor competes for
    # the lock, so let it run freely alongside a real autoland.
    lock: AutolandLock | None = None
    replan = False
    if not opts.dry_run:
        lock = AutolandLock.for_state(state_path)
        if not lock.acquire():
            # A run that has not checkpointed yet has no progress to replan
            # from, so there is nothing to offer but waiting.
            if not state_path.exists():
                runtime.console.print(
                    f"[red]An autoland is already running for branch "
                    f"[bold]{branch}[/bold]. Wait for it to finish before "
                    "starting another.[/red]"
                )
                sys.exit(1)
            if not _stop_running_autoland(lock):
                sys.exit(1)
            replan = True

    worktree: Worktree | None = None
    try:
        # An existing state file means a previous run was interrupted and can be
        # resumed; starting fresh would clobber it, so ask first.
        if lock is not None and state_path.exists() and not replan:
            choice = _ask_replan_or_overwrite(state_path)
            if choice is None:
                runtime.console.print(
                    "[red]Aborted — the previous autoland is untouched.[/red]"
                )
                return
            replan = choice == "replan"
        if replan:
            _replan(common, opts, state_path)
            return

        if opts.branch:
            worktree = Worktree(opts.branch)
            worktree.create()
            runtime.console.print(
                f"[green]Working in temporary worktree for [bold]{opts.branch}"
                "[/bold][/green]\n"
            )

        # Deduce the base now that any worktree has been created and we've
        # switched into it. With --branch, HEAD in the primary checkout points
        # at a different branch, so deducing earlier would freeze a base that
        # isn't an ancestor of the stack. deduce_base honors an explicit --base.
        common = cli.deduce_base(common)

        runtime.console.print("\n[bold]Discovering stack...[/bold]\n")
        stack = discovery.discover_stack(common)
        if not stack:
            runtime.console.print("[red]No stack found on the current branch.[/red]")
            sys.exit(1)
        discovery.enrich_stack(stack)

        if opts.count is not None and not 1 <= opts.count <= len(stack):
            runtime.console.print(
                f"[red]--count must be between 1 and {len(stack)} "
                f"(the stack has {len(stack)} PRs).[/red]"
            )
            sys.exit(1)

        if opts.plan_file is not None:
            plan = plan_from_file(opts.plan_file, stack)
        elif opts.interactive:
            plan = edit_plan_interactive(stack, opts.default_workflow, opts.count)
        else:
            plan = generate_default_plan(stack, count=opts.count)
        ctx = LandingContext(stack=stack, plan=plan)

        checkpointer = AutolandCheckpointer(
            path=state_path,
            branch=branch,
            base=common.target,
            plan_file=opts.plan_file,
        )

        print_status(ctx)
        print_native_stack_runs(ctx, opts)
        if opts.dry_run:
            runtime.console.print("\n[yellow]Dry run — exiting.[/yellow]")
            return

        runtime.console.print(f"[dim]State file: {checkpointer.path}[/dim]\n")
        _install_signal_handler(ctx, checkpointer, worktree, opts)
        _finish(
            ctx,
            checkpointer,
            worktree,
            opts,
            success=engine.execute_plan(ctx, common, opts, checkpointer),
        )
    finally:
        if worktree is not None:
            worktree.remove_unless_landing()
        if lock is not None:
            lock.release()


def _run_resume(common: cli.CommonArgs, opts: AutolandOptions) -> None:
    sf_path = _state_path(opts)

    if not sf_path.exists():
        runtime.console.print(f"[red]No state file found at {sf_path}[/red]")
        sys.exit(1)

    lock = AutolandLock.for_state(sf_path)
    if not lock.acquire():
        runtime.console.print(
            "[red]An autoland is already running for this branch. Wait for it "
            "to finish before resuming.[/red]"
        )
        sys.exit(1)

    worktree: Worktree | None = None
    try:
        runtime.console.print(
            f"[bold]Resuming from checkpoint: [cyan]{sf_path}[/cyan][/bold]\n"
        )
        try:
            checkpointer, ctx = AutolandCheckpointer.load(sf_path)
        except (ValueError, json.JSONDecodeError, KeyError) as e:
            runtime.console.print(f"[red]Failed to load state file: {e}[/red]")
            sys.exit(1)

        if opts.branch and opts.branch != checkpointer.branch:
            runtime.console.print(
                f"[red]--branch {opts.branch} does not match saved branch "
                f"{checkpointer.branch}[/red]"
            )
            sys.exit(1)

        if opts.branch or checkpointer.branch != git.get_current_branch_name():
            worktree = Worktree(opts.branch or checkpointer.branch)
            worktree.create()

        # Deduce the base against the (possibly worktree) HEAD, for the same
        # reason as in _run_fresh: the primary checkout's HEAD may be a
        # different branch than the one being landed.
        common = cli.deduce_base(common)

        runtime.console.print("[dim]Refreshing PR state from GitHub...[/dim]")
        discovery.enrich_stack(ctx.stack)
        ctx.aborted = False
        ctx.abort_reason = ""

        if ctx.current_step >= len(ctx.plan):
            runtime.console.print(
                "[green]All steps already completed — nothing to resume.[/green]"
            )
            checkpointer.delete()
            _dispose_worktree(worktree, opts, succeeded=True)
            return

        print_status(ctx)
        print_native_stack_runs(ctx, opts)
        runtime.console.print(f"[dim]State file: {sf_path}[/dim]\n")
        _install_signal_handler(ctx, checkpointer, worktree, opts)
        _finish(
            ctx,
            checkpointer,
            worktree,
            opts,
            success=engine.execute_plan(ctx, common, opts, checkpointer),
        )
    finally:
        if worktree is not None:
            worktree.remove_unless_landing()
        lock.release()


# ---------------------------------------------------------------------------
# Replanning (`autoland --replan`)
# ---------------------------------------------------------------------------

# How long to wait for a stopped autoland to save its checkpoint and exit.
_TAKEOVER_TIMEOUT = 60


def _stop_running_autoland(lock: AutolandLock) -> bool:
    """Offer to stop the autoland holding *lock*, then take the lock over.

    The other run is sent SIGINT, the same as Ctrl+C in its terminal: it saves
    its checkpoint and exits, releasing the lock. Returns True once this process
    holds *lock*, or False if the user declined or the run could not be stopped.
    """
    pid = lock.holder_pid()
    if pid is None:
        runtime.console.print(
            "[red]An autoland is running for this branch, but its PID is unknown "
            "(it was started by an older stack-pr). Stop it with Ctrl+C in its "
            "terminal, then re-run this command.[/red]"
        )
        return False
    runtime.console.print(
        f"\n[bold yellow]An autoland is running for this branch "
        f"(pid {pid}).[/bold yellow]\n"
        "[yellow]Replanning stops it the way Ctrl+C in its terminal would — it "
        "saves its checkpoint and exits — and continues from there in this "
        "terminal.[/yellow]\n"
    )
    try:
        answer = runtime.console.input(
            "[yellow]Stop it and replan? Type y/Y to confirm (anything else "
            "aborts): [/yellow]"
        ).strip()
    except EOFError:
        answer = ""
    if answer not in ("y", "Y"):
        runtime.console.print("[red]Aborted — the running autoland is untouched.[/red]")
        return False

    try:
        os.kill(pid, signal.SIGINT)
    except ProcessLookupError:
        pass  # it exited on its own meanwhile; the lock is (about to be) free
    except PermissionError as e:
        runtime.console.print(f"[red]Could not stop pid {pid}: {e}[/red]")
        return False

    runtime.console.print(f"[dim]Waiting for pid {pid} to save its checkpoint...[/dim]")
    deadline = time.monotonic() + _TAKEOVER_TIMEOUT
    while time.monotonic() < deadline:
        if lock.acquire():
            return True
        time.sleep(0.5)
    runtime.console.print(
        f"[red]pid {pid} still holds the lock after {_TAKEOVER_TIMEOUT}s. Stop it "
        "in its terminal, then re-run this command.[/red]"
    )
    return False


def _unpushed_changes(raw: list[cli.StackEntry], common: cli.CommonArgs) -> list[str]:
    """Commits in the stack whose code GitHub doesn't have, for a warning.

    Replanning usually follows a code change, and landing a PR whose branch
    predates that change would ship the old code.
    """
    problems = []
    for e in raw:
        name = f"{e.commit.commit_id()[:8]} {e.commit.title()}"
        if not e.has_pr():
            problems.append(f"{name}: no PR yet")
            continue
        pushed = runtime.run(
            ["git", "rev-parse", "--verify", "--quiet", f"{common.remote}/{e.head}"],
            check=False,
            quiet=True,
            retries=0,
        ).stdout.strip()
        if pushed and pushed != e.commit.commit_id():
            problems.append(f"{name}: local commit differs from #{cli.last(e.pr)}")
    return problems


def _describe_lost(old: LandingContext, index: int) -> str:
    """One not-carried-over checkpoint, with the PRs it was recorded after."""
    key = _checkpoint_keys(old.plan, old.stack)[index]
    landed = sorted(key[2]) if key else []
    after = (
        "after " + ", ".join(f"#{pr}" for pr in landed)
        if landed
        else "before any PR landed"
    )
    return f"{_describe_step(old.plan[index], old)} ({after})"


def _replacement_plan(
    opts: AutolandOptions,
    old_checkpointer: AutolandCheckpointer,
    old: LandingContext,
    stack: list[StackEntry],
) -> tuple[list[PlanStep], Path | None]:
    """The new plan for a replan, and the plan file it came from (if any)."""
    if opts.interactive:
        # Start the editor from the plan being replaced, not the default one.
        initial = format_plan_for_editor(old.stack, old.plan)
        return edit_plan_interactive(stack, initial_text=initial), None
    plan_file = opts.plan_file or old_checkpointer.plan_file
    if plan_file is not None:
        runtime.console.print(f"[dim]Plan: {plan_file}[/dim]")
        return plan_from_file(plan_file, stack), plan_file
    # The run had no plan file (a default or -i plan): replay the saved plan.
    try:
        return parse_plan(format_plan_for_editor(old.stack, old.plan), stack), None
    except ValueError as e:
        runtime.console.print(
            f"[red]The saved plan no longer fits the stack: {e}\n"
            "Give a new plan with --plan-file or -i.[/red]"
        )
        sys.exit(1)


def _replan(common: cli.CommonArgs, opts: AutolandOptions, state_path: Path) -> None:
    """Replace the plan of the run checkpointed at *state_path*, keep its
    progress, and continue.

    The caller holds the lock, except on a dry run, which only previews.
    """
    try:
        old_checkpointer, old = AutolandCheckpointer.load(state_path)
    except (OSError, ValueError, KeyError) as e:
        runtime.console.print(f"[red]Failed to load state file {state_path}: {e}[/red]")
        sys.exit(1)
    branch = old_checkpointer.branch
    if opts.branch and opts.branch != branch:
        runtime.console.print(
            f"[red]--branch {opts.branch} does not match saved branch {branch}[/red]"
        )
        sys.exit(1)

    runtime.console.print(
        f"[bold]Replanning from checkpoint: [cyan]{state_path}[/cyan][/bold]"
    )
    worktree: Worktree | None = None
    try:
        if opts.branch or branch != git.get_current_branch_name():
            worktree = Worktree(branch)
            worktree.create()
        # As in _run_fresh: deduce against the (possibly worktree) HEAD.
        common = cli.deduce_base(common)

        # The code may have changed since the checkpoint, so the stack is
        # rediscovered rather than restored.
        runtime.console.print("\n[bold]Rediscovering stack...[/bold]\n")
        raw = cli.get_stack(base=common.base, head=common.head, verbose=common.verbose)
        stack = discovery._stack_entries(raw)
        if not stack:
            runtime.console.print("[red]No stack found on the current branch.[/red]")
            sys.exit(1)
        discovery.enrich_stack(stack)

        plan, plan_file = _replacement_plan(opts, old_checkpointer, old, stack)
        lost = carry_over_progress(old, plan, stack)
        ctx = LandingContext(stack=stack, plan=plan)

        print_status(ctx)
        print_native_stack_runs(ctx, opts)
        if lost:
            runtime.console.print(
                "\n[yellow]Done in the previous run, but not carried over (changed, "
                "removed, or now after different PRs):[/yellow]"
            )
            for index in lost:
                runtime.console.print(
                    f"  - {_escape_markup(_describe_lost(old, index))}"
                )
        unpushed = _unpushed_changes(raw, common)
        if unpushed:
            runtime.console.print(
                "\n[bold yellow]Warning: GitHub doesn't have all of this stack's code. "
                "Run `stack-pr submit` first if you changed it:[/bold yellow]"
            )
            for problem in unpushed:
                runtime.console.print(f"  - {_escape_markup(problem)}")

        if opts.dry_run:
            runtime.console.print("\n[yellow]Dry run — exiting.[/yellow]")
            _dispose_worktree(worktree, opts, succeeded=True)
            return
        try:
            answer = runtime.console.input(
                "\n[yellow]Continue with this plan? Type y/Y to confirm (anything "
                "else aborts): [/yellow]"
            ).strip()
        except EOFError:
            answer = ""
        if answer not in ("y", "Y"):
            runtime.console.print(
                "[red]Aborted — the previous checkpoint is untouched.[/red]"
            )
            _dispose_worktree(worktree, opts, succeeded=True)
            return

        checkpointer = AutolandCheckpointer(
            path=state_path, branch=branch, base=common.target, plan_file=plan_file
        )
        runtime.console.print(f"[dim]State file: {checkpointer.path}[/dim]\n")
        _install_signal_handler(ctx, checkpointer, worktree, opts)
        _finish(
            ctx,
            checkpointer,
            worktree,
            opts,
            success=engine.execute_plan(ctx, common, opts, checkpointer),
        )
    finally:
        if worktree is not None:
            worktree.remove_unless_landing()


def _run_replan(common: cli.CommonArgs, opts: AutolandOptions) -> None:
    state_path = _state_path(opts)
    if not state_path.exists():
        runtime.console.print(
            f"[red]No autoland to replan: no state file at {state_path}. Start "
            "one with --plan-file or -i.[/red]"
        )
        sys.exit(1)
    # A dry run only previews, so it neither competes for the lock nor stops a
    # running autoland: it is how to check a replan before committing to it.
    if opts.dry_run:
        _replan(common, opts, state_path)
        return
    lock = AutolandLock.for_state(state_path)
    if not lock.acquire() and not _stop_running_autoland(lock):
        sys.exit(1)
    try:
        _replan(common, opts, state_path)
    finally:
        lock.release()
