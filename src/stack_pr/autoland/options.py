"""Autoland's options, resolved from flags, then config, then defaults."""

from __future__ import annotations

import argparse
import configparser
from dataclasses import dataclass
from pathlib import Path

# ---------------------------------------------------------------------------
# Defaults (overridable via [autoland] config or flags)
# ---------------------------------------------------------------------------

DEFAULT_POLL_INTERVAL = 120  # seconds
DEFAULT_MAX_CHECK_RETRIES = 3
DEFAULT_MAX_QUEUE_RETRIES = 3
DEFAULT_WORKFLOW_TIMEOUT = 10800  # 3 hours
DEFAULT_MERGE_TIMEOUT = 3600  # 60 minutes


# ---------------------------------------------------------------------------
# Options resolved by precedence: command-line flag, then config, then default
# ---------------------------------------------------------------------------


@dataclass
class AutolandOptions:
    merge_queue: bool
    required_checks: list[str]
    poll_interval: int
    max_check_retries: int
    max_queue_retries: int
    merge_timeout: int
    workflow_timeout: int
    default_workflow: str | None
    count: int | None
    dry_run: bool
    branch: str | None
    interactive: bool
    resume: bool
    state_file: Path | None
    always_cleanup: bool
    plan_file: Path | None = None
    merge_as_stack: bool = True
    replan: bool = False

    @classmethod
    def from_config_and_args(
        cls, config: configparser.ConfigParser, args: argparse.Namespace
    ) -> AutolandOptions:
        def _int(flag_val: int | None, key: str, default: int) -> int:
            if flag_val is not None:
                return flag_val
            return config.getint("autoland", key, fallback=default)

        raw_checks = config.get("autoland", "required_checks", fallback="")
        required_checks = [c.strip() for c in raw_checks.split(",") if c.strip()]

        state_file = getattr(args, "state_file", None)
        # Resolve the plan file to an absolute path now, while the cwd is still
        # the user's invocation directory: autoland may later chdir into a
        # temporary worktree (--branch), where a relative path would not resolve.
        plan_file = getattr(args, "plan_file", None)
        merge_as_stack = getattr(args, "merge_as_stack", None)
        if merge_as_stack is None:
            merge_as_stack = config.getboolean(
                "autoland", "merge_as_stack", fallback=True
            )
        return cls(
            merge_queue=config.getboolean("autoland", "merge_queue", fallback=False),
            required_checks=required_checks,
            poll_interval=_int(
                getattr(args, "poll_interval", None),
                "poll_interval",
                DEFAULT_POLL_INTERVAL,
            ),
            max_check_retries=_int(
                getattr(args, "max_check_retries", None),
                "max_check_retries",
                DEFAULT_MAX_CHECK_RETRIES,
            ),
            max_queue_retries=_int(
                getattr(args, "max_queue_retries", None),
                "max_queue_retries",
                DEFAULT_MAX_QUEUE_RETRIES,
            ),
            merge_timeout=config.getint(
                "autoland", "merge_timeout", fallback=DEFAULT_MERGE_TIMEOUT
            ),
            workflow_timeout=_int(
                getattr(args, "workflow_timeout", None),
                "workflow_timeout",
                DEFAULT_WORKFLOW_TIMEOUT,
            ),
            default_workflow=(
                config.get("autoland", "default_workflow", fallback="").strip() or None
            ),
            count=getattr(args, "count", None),
            dry_run=bool(getattr(args, "dry_run", False)),
            branch=getattr(args, "branch", None),
            interactive=bool(getattr(args, "interactive", False)),
            resume=bool(getattr(args, "resume", False)),
            state_file=Path(state_file) if state_file else None,
            always_cleanup=bool(getattr(args, "always_cleanup", False)),
            plan_file=Path(plan_file).resolve() if plan_file else None,
            merge_as_stack=merge_as_stack,
            replan=bool(getattr(args, "replan", False)),
        )
