"""Console output, sleep/wake resilience, and the retrying shell runner."""

from __future__ import annotations

import re
import subprocess
import time

from stack_pr import shell_commands
from stack_pr.shell_commands import MAX_RETRIES, run_shell_command

# ---------------------------------------------------------------------------
# Output: use rich when available, fall back to plain text otherwise.
# ---------------------------------------------------------------------------

# Matches rich-style markup tags like [bold], [/dim], [red bold] so the
# plain-text console can strip them. Only the style words this package actually
# writes are recognized: a looser pattern also eats bracketed text that came
# from GitHub — a PR titled "[wip] make it fast" would silently lose its tag.
_STYLE_WORDS = (
    "black|blue|bold|cyan|dim|green|italic|magenta|red|white|yellow|underline"
)
_RE_MARKUP = re.compile(rf"\[/?(?:{_STYLE_WORDS})(?: (?:{_STYLE_WORDS}))*\]")


class _PlainConsole:
    """Minimal stand-in for rich.Console that strips markup."""

    def print(self, *args: object, **_kwargs: object) -> None:
        print(*[_RE_MARKUP.sub("", str(a)) for a in args])

    def input(self, prompt: object = "") -> str:
        return input(_RE_MARKUP.sub("", str(prompt)))


try:
    from rich.console import Console, Group
    from rich.padding import Padding
    from rich.text import Text

    HAVE_RICH = True
except ImportError:  # pragma: no cover - exercised only without the extra
    Console = _PlainConsole  # type: ignore[assignment,misc]
    Group = None  # type: ignore[assignment,misc]
    Padding = None  # type: ignore[assignment,misc]
    Text = None  # type: ignore[assignment,misc]
    HAVE_RICH = False

console = Console()


# ---------------------------------------------------------------------------
# Sleep / wake resilience
# ---------------------------------------------------------------------------

_SLEEP_DETECTION_THRESHOLD = 30


def resilient_sleep(seconds: int) -> float:
    """Sleep, detecting system sleep/wake; wait for network after a wake."""
    start = time.monotonic()
    time.sleep(seconds)
    actual = time.monotonic() - start

    sleep_gap = max(0.0, actual - seconds - _SLEEP_DETECTION_THRESHOLD)
    if sleep_gap > 0:
        gap_min = int(sleep_gap) // 60
        gap_sec = int(sleep_gap) % 60
        console.print(
            f"\n[yellow]System sleep detected — machine was suspended "
            f"~{gap_min}m{gap_sec}s. Waiting for network...[/yellow]"
        )
        _wait_for_network()
        console.print("[green]Network is back. Resuming.[/green]\n")

    return sleep_gap


def _wait_for_network(max_wait: int = 120, interval: int = 5) -> None:
    """Block until ``gh api user`` succeeds or *max_wait* seconds elapse."""
    deadline = time.monotonic() + max_wait
    while time.monotonic() < deadline:
        try:
            result = run_shell_command(
                ["gh", "api", "user", "--jq", ".login"],
                quiet=False,
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            if result.returncode == 0:
                return
        except (subprocess.TimeoutExpired, OSError):
            pass
        time.sleep(interval)
    console.print(
        f"[yellow]Warning: network still unreachable after {max_wait}s — "
        "continuing anyway[/yellow]"
    )


# ---------------------------------------------------------------------------
# Shell helpers (with transient-failure retries)
# ---------------------------------------------------------------------------


def _announce_run(cmd: list[str], attempt: int) -> None:
    suffix = "" if attempt == 0 else f"  (attempt {attempt + 1})"
    console.print(f"[dim]$ {' '.join(cmd)}{suffix}[/dim]")


def _announce_retry(attempt: int, retries: int, delay: float) -> None:
    console.print(f"[yellow]  retry {attempt}/{retries} in {delay}s...[/yellow]")


def run(
    cmd: list[str],
    *,
    check: bool = True,
    capture: bool = True,
    quiet: bool = False,
    input_data: bytes | None = None,
    retries: int = MAX_RETRIES,
) -> subprocess.CompletedProcess[str]:
    """Run a subprocess, retrying likely-transient failures.

    See ``shell_commands.run_with_retry``; this adds autoland's console output
    (the command, and each retry) unless *quiet*. Pass ``retries=0`` for a
    command that changes state (a merge, a POST, a rebase): the caller's later
    polling reconciles a request that may have taken effect despite failing.

    Commands run in the current working directory (autoland chdirs into a
    temporary worktree when ``--branch`` is used).
    """
    return shell_commands.run_with_retry(
        cmd,
        check=check,
        capture=capture,
        input_data=input_data,
        retries=retries,
        on_run=None if quiet else _announce_run,
        on_retry=None if quiet else _announce_retry,
    )
