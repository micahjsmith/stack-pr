"""Evaluating a PR's CI checks against the required ones."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum

# ---------------------------------------------------------------------------
# Check monitoring
# ---------------------------------------------------------------------------

RE_RUN_ID_FROM_LINK = re.compile(r"/actions/runs/(\d+)")


def _extract_run_id(link: str) -> int | None:
    m = RE_RUN_ID_FROM_LINK.search(link)
    return int(m.group(1)) if m else None


class CheckStatus(Enum):
    ALL_PASSING = "all_passing"
    PENDING = "pending"
    FAILED = "failed"
    NOT_STARTED = "not_started"


@dataclass
class CheckResult:
    status: CheckStatus
    failed_runs: list[int] = field(default_factory=list)
    failed_names: list[str] = field(default_factory=list)
    summary: str = ""


def evaluate_checks(checks: list[dict], required_checks: list[str]) -> CheckResult:
    """Evaluate a PR's check runs.

    If *required_checks* is non-empty, gate on exactly those named checks.
    Otherwise gate on all reported checks that aren't being skipped.
    """
    if required_checks:
        check_map = {
            c.get("name", ""): c for c in checks if c.get("name", "") in required_checks
        }
        missing = [n for n in required_checks if n not in check_map]
        if missing:
            return CheckResult(
                status=CheckStatus.NOT_STARTED,
                summary=f"Waiting for checks to start: {', '.join(missing)}",
            )
        names = list(required_checks)
    else:
        check_map = {}
        for c in checks:
            if (c.get("bucket") or "").lower() == "skipping":
                continue
            check_map[c.get("name", "")] = c
        if not check_map:
            return CheckResult(
                status=CheckStatus.NOT_STARTED,
                summary="Waiting for checks to start",
            )
        names = list(check_map.keys())

    failed_runs: list[int] = []
    failed_names: list[str] = []
    any_pending = False

    for name in names:
        bucket = (check_map[name].get("bucket") or "").lower()
        if bucket == "pass":
            continue
        if bucket in ("fail", "cancel"):
            run_id = _extract_run_id(check_map[name].get("link", ""))
            if run_id:
                failed_runs.append(run_id)
            failed_names.append(name)
        else:
            # pending, skipping, or unknown -> still waiting
            any_pending = True

    if failed_names:
        return CheckResult(
            status=CheckStatus.FAILED,
            failed_runs=failed_runs,
            failed_names=failed_names,
            summary=f"Failed: {', '.join(failed_names)}",
        )
    if any_pending:
        return CheckResult(status=CheckStatus.PENDING, summary="Checks in progress...")
    return CheckResult(status=CheckStatus.ALL_PASSING, summary="All checks passing")
