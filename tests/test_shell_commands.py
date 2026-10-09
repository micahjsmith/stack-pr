import subprocess
from pathlib import Path

import pytest

from stack_pr import shell_commands
from stack_pr.shell_commands import (
    CommandError,
    CommandFailedError,
    get_command_output,
    run_shell_command,
    run_with_retry,
)


def test_cmd_success_quiet_false_print(capfd: pytest.CaptureFixture) -> None:
    """Test that stdout and stderr are printed when quiet=False on success."""
    # Use a command that produces both stdout and stderr
    # sh -c 'echo "out" && echo "err" >&2' produces both
    result = run_shell_command(
        ["sh", "-c", 'echo "stdout_msg" && echo "stderr_msg" >&2'],
        quiet=False,
    )

    # stdout and stderr are not captured in memory
    assert result.returncode == 0
    assert result.stdout is None
    assert result.stderr is None

    # stdout and stderr are printed to console
    captured = capfd.readouterr()
    assert "stdout_msg" in captured.out
    assert "stderr_msg" in captured.err


def test_cmd_success_quiet_true_captured(capfd: pytest.CaptureFixture) -> None:
    """Test that stdout and stderr are captured when quiet=True on success."""
    result = run_shell_command(
        ["sh", "-c", 'echo "stdout_msg" && echo "stderr_msg" >&2'],
        quiet=True,
    )

    # stdout and stderr are captured in memory
    assert result.returncode == 0
    assert "stdout_msg" in result.stdout.decode("utf-8")
    assert "stderr_msg" in result.stderr.decode("utf-8")

    # stdout and stderr are not printed to console
    captured = capfd.readouterr()
    assert "stdout_msg" not in captured.out
    assert "stderr_msg" not in captured.err


def test_cmd_fail_quiet_true_captured(capfd: pytest.CaptureFixture) -> None:
    """Test that stdout and stderr are caught by exception handling.

    Tests behavior when quiet=True on failure.
    """
    with pytest.raises(subprocess.CalledProcessError) as exc:
        run_shell_command(
            ["sh", "-c", 'echo "stdout_msg" && echo "stderr_msg" >&2 && exit 1'],
            quiet=True,
        )

    # stdout and stderr are captured in exception info
    exception = exc.value
    assert "stdout_msg" in exception.stdout.decode("utf-8")
    assert "stderr_msg" in exception.stderr.decode("utf-8")

    # stdout and stderr are not printed to console
    captured = capfd.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_cmd_fail_quiet_false_print(capfd: pytest.CaptureFixture) -> None:
    """Test that stdout and stderr are printed when quiet=False on failure."""
    with pytest.raises(subprocess.CalledProcessError):
        run_shell_command(
            ["sh", "-c", 'echo "stdout_msg" && echo "stderr_msg" >&2 && exit 1'],
            quiet=False,
        )

    # stdout and stderr are printed to console
    captured = capfd.readouterr()
    assert "stdout_msg" in captured.out
    assert "stderr_msg" in captured.err


def test_run_shell_command_accepts_generator() -> None:
    """A generator command is run with all of its arguments."""
    cmd = (arg for arg in ["sh", "-c", "echo generator_msg"])
    result = run_shell_command(cmd, quiet=True)

    assert result.stdout.decode("utf-8").strip() == "generator_msg"


def test_get_command_output_accepts_generator() -> None:
    """get_command_output runs a generator command with all of its arguments."""
    cmd = (arg for arg in ["sh", "-c", "echo generator_msg"])

    assert get_command_output(cmd) == "generator_msg"


@pytest.fixture
def no_retry_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shell_commands, "RETRY_DELAY", 0)


def _counting_command(tmp_path: Path, script: str) -> tuple[list[str], Path]:
    """A command that appends a line to a file each time it runs, then *script*."""
    count = tmp_path / "count"
    return ["sh", "-c", f'echo x >> "{count}"; {script}'], count


def _runs(count: Path) -> int:
    return len(count.read_text().splitlines())


@pytest.mark.usefixtures("no_retry_delay")
def test_run_with_retry_retries_a_transient_failure(tmp_path: Path) -> None:
    cmd, count = _counting_command(tmp_path, 'echo "HTTP 502" >&2; exit 1')

    with pytest.raises(CommandFailedError) as exc:
        run_with_retry(cmd, retries=2)

    assert _runs(count) == 3
    # Callers that handle run_shell_command's failures handle this one too.
    assert isinstance(exc.value, subprocess.CalledProcessError)
    assert exc.value.returncode == 1
    assert b"HTTP 502" in exc.value.stderr
    assert "HTTP 502" in str(exc.value)


@pytest.mark.usefixtures("no_retry_delay")
def test_run_with_retry_succeeds_once_a_transient_failure_clears(
    tmp_path: Path,
) -> None:
    # Fails with a network-looking error on the first run only.
    cmd, count = _counting_command(
        tmp_path,
        f'[ "$(wc -l < "{tmp_path / "count"}")" -gt 1 ] && echo ok'
        ' || { echo "connection reset" >&2; exit 1; }',
    )

    assert run_with_retry(cmd).stdout.strip() == "ok"
    assert _runs(count) == 2


@pytest.mark.usefixtures("no_retry_delay")
def test_run_with_retry_does_not_retry_a_logical_failure(tmp_path: Path) -> None:
    cmd, count = _counting_command(tmp_path, 'echo "merge conflict" >&2; exit 1')

    with pytest.raises(CommandFailedError):
        run_with_retry(cmd)

    assert _runs(count) == 1


@pytest.mark.usefixtures("no_retry_delay")
def test_run_with_retry_zero_retries_runs_once(tmp_path: Path) -> None:
    cmd, count = _counting_command(tmp_path, 'echo "HTTP 502" >&2; exit 1')

    with pytest.raises(CommandFailedError):
        run_with_retry(cmd, retries=0)

    assert _runs(count) == 1


@pytest.mark.usefixtures("no_retry_delay")
def test_run_with_retry_retries_a_timeout(tmp_path: Path) -> None:
    cmd, count = _counting_command(tmp_path, "sleep 5")

    with pytest.raises(CommandError, match="timed out"):
        run_with_retry(cmd, retries=1, timeout=0.2)

    assert _runs(count) == 2


def test_run_with_retry_unchecked_failure_returns_the_result() -> None:
    result = run_with_retry(["sh", "-c", 'echo "HTTP 502" >&2; exit 3'], check=False)

    assert result.returncode == 3
    assert "HTTP 502" in result.stderr


def test_run_with_retry_missing_executable_is_not_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(shell_commands, "RETRY_DELAY", 60)  # a retry would hang

    with pytest.raises(CommandError) as exc:
        run_with_retry([str(tmp_path / "no-such-command")])

    assert not isinstance(exc.value, CommandFailedError)


@pytest.mark.usefixtures("no_retry_delay")
def test_run_with_retry_reports_each_attempt(tmp_path: Path) -> None:
    cmd, _ = _counting_command(tmp_path, 'echo "HTTP 502" >&2; exit 1')
    events: list[tuple] = []

    with pytest.raises(CommandFailedError):
        run_with_retry(
            cmd,
            retries=1,
            on_run=lambda c, attempt: events.append(("run", attempt)),
            on_retry=lambda n, total, _delay: events.append(("retry", n, total)),
        )

    assert events == [("run", 0), ("retry", 1, 1), ("run", 1)]
