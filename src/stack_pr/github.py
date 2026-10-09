"""Helpers for reading JSON from the ``gh`` CLI, shared by every subcommand."""

from __future__ import annotations

import json
from typing import Any

from stack_pr import shell_commands


class GitHubError(RuntimeError):
    """A ``gh`` call returned output that isn't the JSON we expected."""


def parse_json(stdout: str, what: str) -> Any:  # noqa: ANN401
    """Parse *stdout* of the command described by *what* as JSON."""
    try:
        return json.loads(stdout)
    except json.JSONDecodeError as e:
        raise GitHubError(f"Unexpected output from {what}: {stdout[:200]!r}") from e


def gh_json(
    cmd: list[str], *, retries: int = shell_commands.MAX_RETRIES
) -> dict | list:
    """Run ``gh <cmd>`` and parse its JSON output.

    Reads are retried on likely-transient failures; see
    ``shell_commands.run_with_retry`` for *retries*.

    Raises:
        shell_commands.CommandError: if gh could not be run or failed.
        GitHubError: if gh's output is not a JSON object or list.
    """
    result = shell_commands.run_with_retry(["gh", *cmd], retries=retries)
    data = parse_json(result.stdout, f"gh {' '.join(cmd)}")
    if not isinstance(data, (dict, list)):
        raise GitHubError(f"Unexpected JSON from gh {' '.join(cmd)}: {data!r}")
    return data


def gh_dict(cmd: list[str], *, retries: int = shell_commands.MAX_RETRIES) -> dict:
    """Run a gh command whose output is a JSON object."""
    data = gh_json(cmd, retries=retries)
    if not isinstance(data, dict):
        raise GitHubError(f"Expected a JSON object from gh {' '.join(cmd)}")
    return data


def gh_dicts(
    cmd: list[str], *, retries: int = shell_commands.MAX_RETRIES
) -> list[dict]:
    """Run a gh command whose output is a JSON list of objects."""
    data = gh_json(cmd, retries=retries)
    if not isinstance(data, list) or not all(isinstance(d, dict) for d in data):
        raise GitHubError(f"Expected a JSON list of objects from gh {' '.join(cmd)}")
    return data


def pr_view(
    pr: str | int, fields: str, *, retries: int = shell_commands.MAX_RETRIES
) -> dict:
    """``gh pr view <pr> --json <fields>``, as a dict."""
    return gh_dict(["pr", "view", str(pr), "--json", fields], retries=retries)


def pr_state(pr: str | int, *, retries: int = shell_commands.MAX_RETRIES) -> str:
    """The GitHub state of a PR: ``OPEN``, ``MERGED``, or ``CLOSED``."""
    return str(pr_view(pr, "state", retries=retries).get("state", "OPEN"))
