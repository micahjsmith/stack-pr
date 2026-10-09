from __future__ import annotations

import subprocess
import sys
import time
from collections.abc import Callable, Iterable
from logging import getLogger
from pathlib import Path

if sys.version_info >= (3, 13):
    # Unpack moved to typing
    from typing import Any, Union
else:
    from typing import Union

    from typing_extensions import Any


logger = getLogger(__name__)

ShellCommand = Iterable[Union[str, Path]]


def run_shell_command(
    cmd: ShellCommand,
    *,
    quiet: bool,
    check: bool = True,
    **kwargs: Any,  # noqa: ANN401
) -> subprocess.CompletedProcess:
    """Runs a shell command using the arguments provided.

    This is essentially a wrapper around subprocess.run, with more reasonable
    default arguments, and some debug logging.

    Args:
        cmd: shell command to run.
        check: see subprocess.run for semantics.
        **kwargs: see subprocess.run for semantics
            (https://docs.python.org/3/library/subprocess.html#subprocess.run).

    Returns:
        A subprocess.CompletedProcess object.
    """
    if "shell" in kwargs:
        raise ValueError("shell support has been removed")
    # Materialize once: cmd may be a one-shot iterable such as a generator.
    args = [str(c) for c in cmd]
    if quiet:
        # If quiet, capture stdout and stderr so they are not printed to the console
        # But respects explicit stderr/stdout settings at the call sites
        if "stderr" not in kwargs:
            kwargs["stderr"] = subprocess.PIPE
        if "stdout" not in kwargs:
            kwargs["stdout"] = subprocess.PIPE
    logger.debug("Running: %s", args)
    return subprocess.run(args, **kwargs, check=check)


def get_command_output(
    cmd: ShellCommand,
    **kwargs: Any,  # noqa: ANN401
) -> str:
    """A wrapper over run_shell_command that captures stdout into a string.

    Args:
        cmd: shell command to run.
        **kwargs: see run_shell_command for semantics. Passing capture_output is
            not allowed.

    Returns:
        Captured stdout of the command as a string.

    Raises:
        ValueError: if the capture_output keyword argument is specified.
    """
    if "capture_output" in kwargs:
        raise ValueError("Cannot pass capture_output when using get_command_output")
    proc = run_shell_command(cmd, capture_output=True, quiet=False, **kwargs)
    return str(proc.stdout.decode("utf-8").rstrip())


# Retries for run_with_retry: how many times a likely-transient failure is
# retried, and how long to wait before each retry.
MAX_RETRIES = 2
RETRY_DELAY = 10  # seconds
COMMAND_TIMEOUT = 300  # seconds

# Substrings (of a failed command's output, lowercased) that suggest the failure
# was a network hiccup worth retrying, rather than a logical failure like a
# merge conflict.
_TRANSIENT_INDICATORS = (
    "could not resolve",
    "connection refused",
    "connection reset",
    "timed out",
    "timeout",
    "network is unreachable",
    "temporary failure",
    "ssl",
    "eof",
    "broken pipe",
    "http 5",  # 500, 502, 503, etc.
    "server error",
    "try again",
    "unavailable",
)


class CommandError(RuntimeError):
    """A command run by run_with_retry could not be run, or timed out."""


class CommandFailedError(CommandError, subprocess.CalledProcessError):
    """A command run by run_with_retry exited non-zero.

    It is also a CalledProcessError, so callers that handle failures from
    run_shell_command handle it too. As there, ``stdout`` and ``stderr`` are
    bytes.
    """

    def __init__(self, result: subprocess.CompletedProcess[str]) -> None:
        subprocess.CalledProcessError.__init__(
            self,
            result.returncode,
            result.args,
            (result.stdout or "").encode(),
            (result.stderr or "").encode(),
        )
        stderr = result.stderr.strip() if result.stderr else ""
        self._message = (
            f"Command failed ({result.returncode}): {' '.join(result.args)}\n{stderr}"
        )

    def __str__(self) -> str:
        return self._message


def is_likely_transient(result: subprocess.CompletedProcess[str]) -> bool:
    """Whether a failed command's output suggests a transient (network) error."""
    text = ((result.stderr or "") + (result.stdout or "")).lower()
    return any(ind in text for ind in _TRANSIENT_INDICATORS)


def run_with_retry(
    cmd: list[str],
    *,
    check: bool = True,
    capture: bool = True,
    input_data: bytes | None = None,
    retries: int = MAX_RETRIES,
    timeout: float = COMMAND_TIMEOUT,
    on_run: Callable[[list[str], int], None] | None = None,
    on_retry: Callable[[int, int, float], None] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a command in text mode, retrying likely-transient failures.

    A command is retried, up to *retries* times and RETRY_DELAY seconds apart,
    when it times out, cannot be started for an OS-level reason, or (with
    *check*) exits non-zero with output that looks like a network error. A
    missing executable and any other non-zero exit are not retried.

    Pass ``retries=0`` for a command that changes state (a merge, a POST, a
    rebase): a request that looks like it failed may still have taken effect,
    so sending it again is not safe.

    Args:
        cmd: command to run.
        check: raise CommandFailedError if the command exits non-zero.
        capture: capture stdout and stderr (as text) instead of passing them
            through.
        input_data: bytes to send to the command's stdin.
        retries: how many times to retry a likely-transient failure.
        timeout: seconds to allow each attempt.
        on_run: called with the command and the 0-based attempt number before
            each attempt.
        on_retry: called with the retry number, *retries*, and the delay
            before waiting to retry.

    Returns:
        The completed process of the last attempt.

    Raises:
        CommandFailedError: if *check* and the command exited non-zero.
        CommandError: if the command could not be run or timed out.
    """
    last_err: CommandError | None = None

    for attempt in range(retries + 1):
        if attempt > 0:
            if on_retry is not None:
                on_retry(attempt, retries, RETRY_DELAY)
            time.sleep(RETRY_DELAY)

        if on_run is not None:
            on_run(cmd, attempt)

        try:
            result = run_shell_command(
                cmd,
                quiet=False,
                check=False,
                capture_output=capture,
                text=True,
                input=input_data.decode() if input_data else None,
                timeout=timeout,
            )
        except FileNotFoundError as exc:  # missing executable; not transient
            raise CommandError(f"Command error: {exc}") from exc
        except (subprocess.TimeoutExpired, OSError) as exc:
            last_err = CommandError(f"Command error: {exc}")
            continue

        if check and result.returncode != 0:
            last_err = CommandFailedError(result)
            # Only retry failures that look transient (network), not logical
            # failures like a merge conflict.
            if is_likely_transient(result):
                continue
            raise last_err

        return result

    assert last_err is not None  # noqa: S101 - every retried attempt sets it
    raise last_err
