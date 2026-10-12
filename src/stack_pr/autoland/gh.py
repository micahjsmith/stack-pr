"""GitHub access: every ``gh`` / ``gh api`` call autoland makes lives here."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from stack_pr import github as github_api
from stack_pr.autoland import runtime
from stack_pr.github import GitHubError, gh_dict, gh_dicts, gh_json, parse_json
from stack_pr.shell_commands import MAX_RETRIES

# ---------------------------------------------------------------------------
# GitHub access — every `gh` / `gh api` call autoland makes lives here.
# ---------------------------------------------------------------------------

_MERGE_QUEUE_ENTRY_QUERY = """
query($owner: String!, $repo: String!, $number: Int!) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $number) {
      mergeQueueEntry { id state }
    }
  }
}
""".strip()

_MERGE_QUEUE_QUERY = """
query($owner: String!, $repo: String!, $branch: String!) {
  repository(owner: $owner, name: $repo) {
    mergeQueue(branch: $branch) { id }
  }
}
""".strip()


@dataclass
class MergeQueuePollResult:
    merged: bool = False
    booted: bool = False
    error: str = ""


class GitHub:
    """Wrapper over the ``gh`` CLI for the PR / merge-queue calls autoland needs."""

    def __init__(self) -> None:
        self._owner_repo: tuple[str, str] | None = None

    def owner_repo(self) -> tuple[str, str]:
        if self._owner_repo is None:
            data = gh_dict(["repo", "view", "--json", "owner,name"])
            self._owner_repo = (data["owner"]["login"], data["name"])
        return self._owner_repo

    def _pr_view(self, pr_number: int, fields: str) -> dict:
        return github_api.pr_view(pr_number, fields)

    def pr_state(self, pr_number: int) -> str:
        return github_api.pr_state(pr_number)

    def merge_state(self, pr_number: int) -> dict:
        return self._pr_view(pr_number, "state,mergeStateStatus,mergeable")

    def review_decision(self, pr_number: int) -> str:
        data = self._pr_view(pr_number, "reviewDecision")
        return str(data.get("reviewDecision") or "")

    def summary(self, pr_number: int) -> dict:
        return self._pr_view(pr_number, "title,state,reviewDecision")

    def checks(self, pr_number: int) -> list[dict]:
        return gh_dicts(
            [
                "pr",
                "checks",
                str(pr_number),
                "--json",
                "name,state,bucket,link,workflow",
            ]
        )

    def rerun_failed(self, run_ids: list[int]) -> None:
        for run_id in dict.fromkeys(run_ids):  # de-dup, preserve order
            try:
                runtime.run(
                    ["gh", "run", "rerun", str(run_id), "--failed"],
                    quiet=False,
                    retries=0,
                )
            except RuntimeError as e:
                runtime.console.print(
                    f"[yellow]Warning: could not rerun {run_id}: {e}[/yellow]"
                )

    def in_merge_queue(self, pr_number: int) -> bool | None:
        """Whether the PR currently has an active merge-queue entry (GraphQL).

        ``None`` when the lookup failed, so it's unknown whether the PR is queued.
        """
        try:
            owner, repo = self.owner_repo()
            result = runtime.run(
                [
                    "gh",
                    "api",
                    "graphql",
                    "-F",
                    f"owner={owner}",
                    "-F",
                    f"repo={repo}",
                    "-F",
                    f"number={pr_number}",
                    "-f",
                    f"query={_MERGE_QUEUE_ENTRY_QUERY}",
                ],
                quiet=True,
            )
            entry = (
                json.loads(result.stdout)
                .get("data", {})
                .get("repository", {})
                .get("pullRequest", {})
                .get("mergeQueueEntry")
            )
        except (RuntimeError, json.JSONDecodeError, AttributeError):
            return None
        return entry is not None

    def enqueue(self, pr_number: int) -> None:
        runtime.run(
            ["gh", "pr", "merge", str(pr_number), "--squash"], quiet=False, retries=0
        )

    # GitHub's native stacked PRs. A stack is an explicit server-side object —
    # GitHub does not infer one from a chain of PR bases — and once a PR is in
    # one, only the asynchronous merge API can merge it. See
    # https://docs.github.com/en/rest/pulls/stacks and
    # https://docs.github.com/en/rest/pulls/pulls#merge-a-pull-request-asynchronously

    def _api(self, method: str, path: str, body: dict | None = None) -> Any:  # noqa: ANN401
        owner, repo = self.owner_repo()
        cmd = ["gh", "api", "-X", method, f"repos/{owner}/{repo}/{path}"]
        if body is not None:
            cmd += ["--input", "-"]
        result = runtime.run(
            cmd,
            quiet=True,
            input_data=json.dumps(body).encode() if body is not None else None,
            retries=MAX_RETRIES if method == "GET" else 0,
        )
        if not result.stdout.strip():
            return None
        return parse_json(result.stdout, f"gh api {method} {path}")

    def find_native_stack(self, pr_number: int) -> dict | None:
        """The GitHub stack *pr_number* belongs to, or ``None`` if it has none."""
        stacks = self._api("GET", f"stacks?pull_request={pr_number}")
        return stacks[0] if isinstance(stacks, list) and stacks else None

    def create_native_stack(self, pr_numbers: list[int]) -> dict:
        """Register *pr_numbers* (bottom first) as a GitHub stack."""
        stack = self._api("POST", "stacks", {"pull_requests": pr_numbers})
        if not isinstance(stack, dict):
            raise GitHubError(f"Unexpected response creating a stack: {stack!r}")
        return stack

    def unstack_native_stack(self, native_stack_number: int) -> None:
        """Remove a stack's unmerged PRs from it; queued PRs stay queued."""
        self._api("POST", f"stacks/{native_stack_number}/unstack")

    def set_base(self, pr_number: int, base: str) -> None:
        runtime.run(
            ["gh", "pr", "edit", str(pr_number), "--base", base], quiet=True, retries=0
        )

    def has_merge_queue(self, branch: str) -> bool | None:
        """Whether *branch* has a merge queue, or ``None`` if GitHub can't say."""
        try:
            owner, repo = self.owner_repo()
            result = runtime.run(
                [
                    "gh",
                    "api",
                    "graphql",
                    "-F",
                    f"owner={owner}",
                    "-F",
                    f"repo={repo}",
                    "-F",
                    f"branch={branch}",
                    "-f",
                    f"query={_MERGE_QUEUE_QUERY}",
                ],
                quiet=True,
            )
            repository = json.loads(result.stdout)["data"]["repository"]
        except (RuntimeError, json.JSONDecodeError, KeyError, TypeError):
            return None
        return repository.get("mergeQueue") is not None

    def merge_async(self, pr_number: int, *, merge_queue: bool) -> str | None:
        """Request a merge of *pr_number* and every open PR below it in its stack.

        Returns the request's id for ``merge_async_status``, or ``None`` when
        GitHub didn't return one (the PR was already merged or queued).
        """
        # The merge queue merges with its own configured method, and GitHub
        # rejects a request that names one ("Custom merge params are not
        # supported when merging via a merge queue").
        body = (
            {"merge_action": "merge_queue"}
            if merge_queue
            else {"merge_action": "direct_merge", "merge_method": "squash"}
        )
        data = self._api("PUT", f"pulls/{pr_number}/merge-async", body)
        if not isinstance(data, dict):
            return None
        details = data.get("details")
        uuid = details.get("uuid") if isinstance(details, dict) else None
        return uuid or data.get("uuid") or None

    def merge_async_status(self, pr_number: int, uuid: str) -> tuple[str, str]:
        """``(status, message)`` of a merge request.

        Status is one of ``pending``, ``enqueued``, ``merged``, ``failed``.
        """
        data = self._api("GET", f"pulls/{pr_number}/merge-async/{uuid}")
        if not isinstance(data, dict):
            return "", ""
        details = data.get("details")
        message = details.get("message", "") if isinstance(details, dict) else ""
        return data.get("status", ""), message or ""

    def poll_merge(self, pr_number: int) -> MergeQueuePollResult:
        state = self.pr_state(pr_number)
        if state == "MERGED":
            return MergeQueuePollResult(merged=True)
        if state == "CLOSED":
            return MergeQueuePollResult(error="PR was closed")
        # An unknown queue status (a failed lookup) is not a boot; poll again.
        if state == "OPEN" and self.in_merge_queue(pr_number) is False:
            return MergeQueuePollResult(booted=True)
        return MergeQueuePollResult()

    def workflow_runs(self, workflow: str, branch: str) -> list[dict]:
        return gh_dicts(
            [
                "run",
                "list",
                "--workflow",
                workflow,
                "--branch",
                branch,
                "--json",
                "headSha,status,conclusion",
                "--limit",
                "10",
            ]
        )

    def merge_commit(self, pr_number: int) -> str | None:
        """Return the SHA of the commit ``pr_number`` merged as, if any."""
        try:
            data = gh_json(["pr", "view", str(pr_number), "--json", "mergeCommit"])
        except RuntimeError:
            return None
        if isinstance(data, dict):
            merge = data.get("mergeCommit")
            if isinstance(merge, dict):
                return merge.get("oid") or None
        return None

    def contains(self, ancestor: str, descendant: str) -> bool | None:
        """Whether ``ancestor`` is an ancestor of ``descendant``, per GitHub.

        Asks the compare API for the merge base of the two commits, so this
        works for commits that were never fetched into the local clone.
        Returns ``None`` if GitHub could not answer (network error, or a commit
        it does not know either) — that is "unknown", not "no".
        """
        try:
            owner, repo = self.owner_repo()
            # per_page=1 trims the (unused) commit list in the response.
            path = f"repos/{owner}/{repo}/compare/{ancestor}...{descendant}"
            result = runtime.run(
                ["gh", "api", f"{path}?per_page=1", "--jq", ".merge_base_commit.sha"],
                quiet=True,
            )
        except RuntimeError:
            return None
        merge_base = result.stdout.strip()
        if not merge_base or merge_base == "null":  # jq prints null for a miss
            return None
        # ancestor is an ancestor of descendant exactly when it *is* the merge
        # base of the two (this also covers the identical-commit case).
        return _sha_eq(merge_base, ancestor)


github = GitHub()


# git's own minimum abbreviation length
_MIN_SHA_LEN = 7


def _sha_eq(a: str, b: str) -> bool:
    """Whether two (possibly abbreviated) SHAs name the same commit.

    Comparison is on the shorter SHA's length; anything shorter than
    ``_MIN_SHA_LEN`` is too ambiguous to call equal, so it is reported as
    different.
    """
    n = min(len(a), len(b))
    if n < _MIN_SHA_LEN:
        return False
    return a[:n].lower() == b[:n].lower()
