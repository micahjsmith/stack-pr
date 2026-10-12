"""Interrupting a landing run: Ctrl+C, or a takeover's SIGINT.

Either way the run sees KeyboardInterrupt wherever it happens to be (a wait,
a confirm prompt), and the run boundary checkpoints it, exits 130, and
releases the lock, so ``--resume`` carries on from the interrupted step.
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pytest
from pytest_mock import MockerFixture

from stack_pr.autoland.commands import _run_resume, _stop_running_autoland
from stack_pr.autoland.model import (
    ConfirmStep,
    LandingContext,
    LandStep,
    PRState,
    StackEntry,
)
from stack_pr.autoland.services import Services
from stack_pr.autoland.state import AutolandCheckpointer, AutolandLock
from stack_pr.autoland.steps import AutolandStep
from stack_pr.autoland.worktree import Worktree
from tests.autoland_fakes import OPTS, FakeRepo, FakeUI, FakeWaits, stack
from tests.helpers import common_args
from tests.test_autoland_step_execution import services

INTERRUPTED = "User interrupted (Ctrl+C)"
REPO_ROOT = Path(__file__).resolve().parents[1]


def _save(path: Path, plan: list[AutolandStep]) -> None:
    ctx = LandingContext(stack=stack(101, 102), plan=plan)
    AutolandCheckpointer(path=path, branch="feat", base="main").save(ctx)


def _land_both() -> list[AutolandStep]:
    return [
        LandStep(entry_index=0, pr_number=101),
        LandStep(entry_index=1, pr_number=102),
    ]


def _waits(approved: list[int], *, interrupt_at: int | None = None) -> FakeWaits:
    """Waits that record each approved PR, and see Ctrl+C while waiting for
    *interrupt_at*'s approval."""

    def approval(entry: StackEntry) -> bool:
        if entry.pr_number == interrupt_at:
            raise KeyboardInterrupt
        approved.append(entry.pr_number)
        return True

    return FakeWaits(approval=approval)


class InterruptedUI(FakeUI):
    """Ctrl+C at the first prompt."""

    def input(self, prompt: str) -> str:
        self.lines.append(prompt)
        raise KeyboardInterrupt


@pytest.fixture
def resume_io(mocker: MockerFixture) -> None:
    """Stub the git side of a resume, and the refresh of its PRs."""
    mocker.patch("stack_pr.git.get_current_branch_name", return_value="feat")
    mocker.patch("stack_pr.cli.deduce_base", side_effect=lambda c: c)
    mocker.patch("stack_pr.autoland.discovery.enrich_stack")


@pytest.fixture
def worktree_events(mocker: MockerFixture) -> list[str]:
    """Replace the --branch worktree with one that records what happens to it."""
    events: list[str] = []

    class FakeWorktree(Worktree):
        def create(self) -> None:
            self.path = Path("worktree")

        def remove(self) -> None:
            events.append("removed")

        def announce_preserved(self) -> None:
            events.append("preserved")

    mocker.patch("stack_pr.autoland.commands.Worktree", FakeWorktree)
    return events


def _resume(sf: Path, svc: Services, **overrides: object) -> None:
    _run_resume(
        common_args(),
        replace(OPTS, resume=True, state_file=sf, **overrides),
        services=svc,
    )


def _printed(console: Mock) -> str:
    return "\n".join(str(a) for c in console.print.call_args_list for a in c.args)


def _assert_interrupted(sf: Path, console: Mock, *, at_step: int) -> LandingContext:
    _, saved = AutolandCheckpointer.load(sf)
    assert saved.abort_reason == INTERRUPTED
    assert saved.current_step == at_step
    assert "Interrupted! State saved. Resume with --resume." in _printed(console)
    # The lock is free for the --resume.
    lock = AutolandLock.for_state(sf)
    assert lock.acquire()
    lock.release()
    return saved


@pytest.mark.parametrize(
    ("always_cleanup", "worktree"),
    [(False, ["preserved"]), (True, ["removed"])],
    ids=["keeps-worktree", "always-cleanup"],
)
@pytest.mark.usefixtures("resume_io")
def test_ctrl_c_during_a_wait_checkpoints_and_exits_130(
    tmp_path: Path,
    autoland_console: Mock,
    *,
    worktree_events: list[str],
    always_cleanup: bool,
    worktree: list[str],
) -> None:
    sf = tmp_path / "state.json"
    _save(sf, _land_both())
    svc = services(waits=_waits([], interrupt_at=102), repo=FakeRepo())

    with pytest.raises(SystemExit) as exc:
        _resume(sf, svc, branch="feat", always_cleanup=always_cleanup)

    assert exc.value.code == 130
    saved = _assert_interrupted(sf, autoland_console, at_step=1)
    assert saved.stack[0].state == PRState.MERGED
    # Kept after an interruption, like after any failure, unless asked not to.
    assert worktree_events == worktree


@pytest.mark.usefixtures("resume_io")
def test_ctrl_c_at_a_confirm_prompt_checkpoints_and_exits_130(
    tmp_path: Path, autoland_console: Mock
) -> None:
    sf = tmp_path / "state.json"
    _save(sf, [LandStep(entry_index=0, pr_number=101), ConfirmStep()])

    with pytest.raises(SystemExit) as exc:
        _resume(sf, services(InterruptedUI(), repo=FakeRepo()))

    assert exc.value.code == 130
    saved = _assert_interrupted(sf, autoland_console, at_step=1)
    confirm = saved.plan[1]
    assert isinstance(confirm, ConfirmStep)
    assert not confirm.confirmed


@pytest.mark.usefixtures("autoland_console", "resume_io")
def test_resume_after_ctrl_c_continues_from_the_interrupted_step(
    tmp_path: Path,
) -> None:
    sf = tmp_path / "state.json"
    _save(sf, _land_both())
    approved: list[int] = []
    with pytest.raises(SystemExit):
        _resume(sf, services(waits=_waits(approved, interrupt_at=102), repo=FakeRepo()))
    assert approved == [101]

    _resume(sf, services(waits=_waits(approved), repo=FakeRepo()))

    # #101 isn't landed again; #102 is landed from the start of its step.
    assert approved == [101, 102]
    assert not sf.exists()


# A run blocked in a wait in its own process, started with SIGINT ignored the
# way a non-interactive shell starts a background job.
_BLOCKED_RUN = """
import signal, sys, time
from dataclasses import replace
from pathlib import Path
from unittest import mock

signal.signal(signal.SIGINT, signal.SIG_IGN)

from stack_pr.autoland import commands
from tests.autoland_fakes import OPTS, FakeRepo, FakeWaits
from tests.helpers import common_args
from tests.test_autoland_step_execution import services

def block(entry):
    print("waiting", flush=True)
    time.sleep(60)
    return True

sf = Path({path!r})
with mock.patch("stack_pr.git.get_current_branch_name", return_value="feat"), \\
        mock.patch("stack_pr.cli.deduce_base", side_effect=lambda c: c), \\
        mock.patch("stack_pr.autoland.discovery.enrich_stack"):
    commands._run_resume(
        common_args(),
        replace(OPTS, resume=True, state_file=sf),
        services=services(waits=FakeWaits(approval=block), repo=FakeRepo()),
    )
"""


def test_takeover_stops_a_run_blocked_in_a_wait(
    tmp_path: Path, autoland_console: Mock
) -> None:
    sf = tmp_path / "state.json"
    _save(sf, _land_both())
    run = subprocess.Popen(
        [sys.executable, "-c", _BLOCKED_RUN.format(path=str(sf))],
        cwd=REPO_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        assert run.stdout is not None
        for line in run.stdout:
            if line.strip() == "waiting":
                break
        autoland_console.input.return_value = "y"
        lock = AutolandLock.for_state(sf)

        assert _stop_running_autoland(lock)
        lock.release()

        output, _ = run.communicate(timeout=10)
        assert run.returncode == 130, output
        assert "Interrupted! State saved. Resume with --resume." in output
        _, saved = AutolandCheckpointer.load(sf)
        assert saved.abort_reason == INTERRUPTED
        assert saved.current_step == 0
    finally:
        run.kill()
        run.wait()
