# stack-pr: a tool for working with stacked PRs on github.
#
# ---------------
# stack-pr submit
# ---------------
#
# Semantics:
#  1. Find merge-base (the most recent commit from 'main' in the current branch)
#  2. For each commit since merge base do:
#       a. If it doesnt have stack info:
#           - create a new head branch for it
#           - create a new PR for it
#           - base branch will be the previous commit in the stack
#       b. If it has stack info: verify its correctness.
#  3. Make sure all commits in the stack are annotated with stack info
#  4. Push all the head branches
#
# If 'submit' succeeds, you'll get all commits annotated with links to the
# corresponding PRs and names of the head branches. All the branches will be
# pushed to remote, and PRs are properly created and interconnected. Base
# branch of each PR will be the head branch of the previous PR, or 'main' for
# the first PR in the stack.
#
# -------------
# stack-pr land
# -------------
#
# Semantics:
#  1. Find merge-base (the most recent commit from 'main' in the current branch)
#  2. Check that all commits in the stack have stack info. If not, bail.
#  3. Check that the stack info is valid. If not, bail.
#  4. For each commit in the stack, from oldest to newest:
#     - set base branch to point to main
#     - merge the corresponding PR
#
# If 'land' succeeds, all the PRs from the stack will be merged into 'main',
# all the corresponding remote and local branches deleted.
#
# ----------------
# stack-pr abandon
# ----------------
#
# Semantics:
# For all commits in the stack that have valid stack-info:
# Close the corresponding PR, delete the remote and local branch, remove the
# stack-info from commit message.
#
# ===----------------------------------------------------------------------=== #

from __future__ import annotations

import argparse
import configparser
import json
import logging
import os
import re
import sys
from dataclasses import dataclass
from functools import cache
from logging import getLogger
from pathlib import Path
from re import Pattern
from subprocess import PIPE, SubprocessError

from stack_pr.git import (
    branch_exists,
    check_gh_installed,
    get_current_branch_name,
    get_gh_username,
    get_repo_root,
    get_uncommitted_changes,
    is_rebase_in_progress,
)
from stack_pr.shell_commands import (
    get_command_output,
    run_shell_command,
)

logger = getLogger(__name__)

# Global verbose flag
_verbose = False


def set_verbose(verbose: bool) -> None:  # noqa: FBT001
    """Set the global verbose flag."""
    global _verbose  # noqa: PLW0603
    _verbose = verbose


def is_verbose() -> bool:
    """Check if verbose mode is enabled."""
    return _verbose


# A bunch of regexps for parsing commit messages and PR descriptions
RE_RAW_COMMIT_ID = re.compile(r"^(?P<commit>[a-f0-9]+)$", re.MULTILINE)
RE_RAW_AUTHOR = re.compile(
    r"^author (?P<author>(?P<name>[^<]+?) <(?P<email>[^>]+)>)", re.MULTILINE
)
RE_RAW_PARENT = re.compile(r"^parent (?P<commit>[a-f0-9]+)$", re.MULTILINE)
RE_RAW_TREE = re.compile(r"^tree (?P<tree>.+)$", re.MULTILINE)
RE_RAW_COMMIT_MSG_LINE = re.compile(r"^    (?P<line>.*)$", re.MULTILINE)

# stack-info: PR: https://github.com/modularml/test-ghstack/pull/30, branch: mvz/stack/7
RE_STACK_INFO_LINE = re.compile(
    r"\n^stack-info: PR: (.+), branch: (.+)\n?", re.MULTILINE
)
RE_PR_TOC = re.compile(
    r"^Stacked PRs:\r?\n(^ \* (__->__)?#\d+\r?\n)*\r?\n", re.MULTILINE
)
# A single entry in the cross-links table of contents, e.g. " * __->__#42".
RE_TOC_ENTRY = re.compile(r"^ \* (?:__->__)?#(\d+)\s*$", re.MULTILINE)

# Header line introducing the cross-links table of contents.
STACK_TOC_HEADER = "Stacked PRs:"

# Delimeter for PR body
CROSS_LINKS_DELIMETER = "--- --- ---"

# ===----------------------------------------------------------------------=== #
# Error message templates
# ===----------------------------------------------------------------------=== #
ERROR_CANT_UPDATE_META = """Couldn't update stack metadata for
    {e}
"""
ERROR_CANT_CREATE_PR = """Could not create a new PR for:
    {e}

Failed trying to execute {cmd}
"""
ERROR_CANT_REBASE = """Could not rebase the PR on '{target}'. Failed to land PR:
    {e}

Failed trying to execute {cmd}
"""
ERROR_STALE_REMOTE_BRANCHES = """Refused to overwrite remote changes on: {branches}

These PR branches have commit(s) on the remote that aren't in your local stack,
so stack-pr stopped instead of force-overwriting them. This usually means a
commit was added to a PR outside of stack-pr — most often a "Commit suggestion"
accepted during review, or an edit made through the GitHub web UI.

Reconcile by folding the upstream commit into the local commit that backs the
PR, then re-submit. See "Reconcile upstream changes" in the README:
    git fetch {remote}
    git rebase -i {remote}/{target}   # mark the affected commit 'edit'
    git cherry-pick -n {remote}/<branch> && git commit --amend --no-edit
    git rebase --continue
    stack-pr submit
"""
ERROR_CANT_CHECKOUT_REMOTE_BRANCH = """Could not checkout remote branch '{e.head}'. Failed to land PR:
    {e}

Failed trying to execute {cmd}
"""
ERROR_STACKINFO_MISSING = """A stack entry is missing some information:
    {e}

If you wanted to land a part of the stack, please use -B and -H options to
specify base and head revisions.
If you wanted to land the entire stack, please use 'submit' first.
If you hit this error trying to submit, please report a bug!
"""
ERROR_STACKINFO_BAD_LINK = """Bad PR link in stack metadata!
    {e}
"""
ERROR_STACKINFO_MALFORMED_RESPONSE = """Malformed response from GH!

Returned json object is missing a field {required_field}
PR info from github: {d}

Failed verification for:
     {e}
"""
ERROR_STACKINFO_PR_NOT_OPEN = """Associated PR is not in 'OPEN' state!
     {e}

PR info from github: {d}
"""
ERROR_STACKINFO_PR_NUMBER_MISMATCH = """PR number on github mismatches PR number in stack metadata!
     {e}

PR info from github: {d}
"""
ERROR_STACKINFO_PR_HEAD_MISMATCH = """Head branch name on github mismatches head branch name in stack metadata!
     {e}

PR info from github: {d}
"""
ERROR_STACKINFO_PR_BASE_MISMATCH = """Base branch name on github mismatches base branch name in stack metadata!
     {e}

If you are trying land the stack, please update it first by calling 'submit'.

PR info from github: {d}
"""
ERROR_STACKINFO_PR_NOT_MERGEABLE = """Associated PR is not mergeable on GitHub!
     {e}

Please fix the issues on GitHub.

PR info from github: {d}
"""
ERROR_REPO_DIRTY = """There are uncommitted changes.

Please commit or stash them before working with stacks.
"""
ERROR_REBASE_IN_PROGRESS = """Cannot submit while in the middle of a rebase.

Please complete or abort the current rebase first.
"""
ERROR_CONFIG_INVALID_FORMAT = """Invalid config format.

Usage: stack-pr config <section>.<key>=<value>

Examples:
  stack-pr config common.verbose=True
  stack-pr config repo.target=main
  stack-pr config repo.reviewer=user1,user2
"""
ERROR_TARGET_BRANCH_MASTER_INSTEAD_OF_MAIN = """Could not find target branch '{remote}/{target}'.

It looks like your repository uses '{remote}/master' instead of '{remote}/main'.

You can fix this by specifying the target branch:
  stack-pr view --target=master

Or set it permanently in your config file:
  stack-pr config repo.target=master
"""
ERROR_TARGET_BRANCH_MISSING = """Could not find target branch '{remote}/{target}'.

Make sure the branch exists or specify a different target with --target option.
"""
UPDATE_STACK_TIP = """
If you'd like to push your local changes first, you can use the following command to update the stack:
  $ stack-pr export -B {top_commit}~{stack_size} -H {top_commit}"""
EXPORT_STACK_TIP = """
You can use the following command to do that:
  $ stack-pr export -B {top_commit}~{stack_size} -H {top_commit}
"""
LAND_STACK_TIP = """
To land it, you could run:
  $ stack-pr land -B {top_commit}~{stack_size} -H {top_commit}

If you'd like to land stack except the top N commits, you could use the following command:
  $ stack-pr land -B {top_commit}~{stack_size} -H {top_commit}~N

If you prefer to merge via the github web UI, please don't forget to edit commit message on the merge page!
If you use the default commit message filled by the web UI, links to other PRs from the stack will be included in the commit message.
"""
ADOPT_TIP = """
The bottom commit is now linked to the adopted PR. To verify and apply:
  $ stack-pr view     # confirm the stack looks right
  $ stack-pr submit   # update the adopted PR and push the rest of the stack
"""
ERROR_ADOPT_ALREADY_MANAGED = """The bottom commit is already managed by stack-pr:
    {e}

There is nothing to adopt. Use 'submit' to update the stack.
"""
ERROR_ADOPT_NO_PR = """Could not find a PR to adopt.

Run 'stack-pr adopt' while checked out on the branch whose PR you want to
adopt, or pass the PR explicitly:
  $ stack-pr adopt <pr-number-or-url>

Failed trying to execute {cmd}
"""
ERROR_ADOPT_PR_NOT_OPEN = """Cannot adopt PR {pr}: it is in '{state}' state, not 'OPEN'.

Only open PRs can be brought under stack-pr management.
"""
ERROR_ADOPT_BAD_COMMIT = """Could not resolve commit '{commit}'.

Failed trying to execute {cmd}
"""
ERROR_ADOPT_COMMIT_NOT_IN_STACK = """Commit '{commit}' ({sha}) is not part of the current stack.

The commit to adopt must be one of the commits in the range being inspected
(use 'stack-pr view' to see them, and -B/-H to adjust the range).
"""
WARN_ADOPT_CONTENT_DIFFERS = """
Warning: the local bottom commit's contents differ from the head of PR {pr}.
The next 'stack-pr submit' will force-push the local commit and update the PR's
diff accordingly. Double-check the commit content before submitting.
"""


# ===----------------------------------------------------------------------=== #
# Class to work with git commit contents
# ===----------------------------------------------------------------------=== #
@dataclass
class CommitHeader:
    """
    Represents the information extracted from `git rev-list --header`
    """

    # The unparsed output from git rev-list --header
    raw_header: str

    def _search_group(self, regex: Pattern[str], group: str) -> str:
        m = regex.search(self.raw_header)
        if m is None:
            raise ValueError(
                f"Required field '{group}' not found in commit header: {self.raw_header}"
            )
        return m.group(group)

    def tree(self) -> str:
        return self._search_group(RE_RAW_TREE, "tree")

    def title(self) -> str:
        return self._search_group(RE_RAW_COMMIT_MSG_LINE, "line")

    def commit_id(self) -> str:
        return self._search_group(RE_RAW_COMMIT_ID, "commit")

    def parents(self) -> list[str]:
        return [m.group("commit") for m in RE_RAW_PARENT.finditer(self.raw_header)]

    def author(self) -> str:
        return self._search_group(RE_RAW_AUTHOR, "author")

    def author_name(self) -> str:
        return self._search_group(RE_RAW_AUTHOR, "name")

    def author_email(self) -> str:
        return self._search_group(RE_RAW_AUTHOR, "email")

    def commit_msg(self) -> str:
        return "\n".join(
            m.group("line") for m in RE_RAW_COMMIT_MSG_LINE.finditer(self.raw_header)
        )


# ===----------------------------------------------------------------------=== #
# Class to work with PR stack entries
# ===----------------------------------------------------------------------=== #
@dataclass
class StackEntry:
    """
    Represents an entry in a stack of PRs and contains associated info, such as
    linked PR, head and base branches, original git commit.
    """

    commit: CommitHeader
    _pr: str | None = None
    _base: str | None = None
    _head: str | None = None

    @property
    def pr(self) -> str:
        if self._pr is None:
            raise ValueError("pr is not set")
        return self._pr

    @pr.setter
    def pr(self, pr: str) -> None:
        self._pr = pr

    def has_pr(self) -> bool:
        return self._pr is not None

    @property
    def head(self) -> str:
        if self._head is None:
            raise ValueError("head is not set")
        return self._head

    @head.setter
    def head(self, head: str) -> None:
        self._head = head

    def has_head(self) -> bool:
        return self._head is not None

    @property
    def base(self) -> str | None:
        return self._base

    @base.setter
    def base(self, base: str | None) -> None:
        self._base = base

    def has_base(self) -> bool:
        return self._base is not None

    def has_missing_info(self) -> bool:
        return None in (self._pr, self._head, self._base)

    def pprint(self, *, links: bool) -> str:
        s = b(self.commit.commit_id()[:8])
        pr_string = None
        pr_string = blue("#" + last(self.pr)) if self.has_pr() else red("no PR")
        branch_string = None
        if self._head or self._base:
            head_str = green(self._head) if self._head else red(str(self._head))
            base_str = green(self._base) if self._base else red(str(self._base))
            branch_string = f"'{head_str}' -> '{base_str}'"
        if pr_string or branch_string:
            s += " ("
        s += pr_string if pr_string else ""
        if branch_string:
            s += ", " if pr_string else ""
            s += branch_string
        if pr_string or branch_string:
            s += ")"
        s += ": " + self.commit.title()

        if links and self.has_pr():
            s = link(self.pr, s)

        return s

    def __repr__(self) -> str:
        return self.pprint(links=False)

    def read_metadata(self) -> None:
        self.commit.commit_msg()
        x = RE_STACK_INFO_LINE.search(self.commit.commit_msg())
        if not x:
            return
        self.pr = x.group(1)
        self.head = x.group(2)


# ===----------------------------------------------------------------------=== #
# Utils for color printing
# ===----------------------------------------------------------------------=== #


class ShellColors:
    HEADER = "\033[95m"
    OKBLUE = "\033[94m"
    OKCYAN = "\033[96m"
    OKGREEN = "\033[92m"
    WARNING = "\033[93m"
    FAIL = "\033[91m"
    ENDC = "\033[0m"
    BOLD = "\033[1m"
    UNDERLINE = "\033[4m"


def b(s: str) -> str:
    return ShellColors.BOLD + s + ShellColors.ENDC


def h(s: str) -> str:
    return ShellColors.HEADER + s + ShellColors.ENDC


def green(s: str) -> str:
    return ShellColors.OKGREEN + s + ShellColors.ENDC


def blue(s: str) -> str:
    return ShellColors.OKBLUE + s + ShellColors.ENDC


def red(s: str) -> str:
    return ShellColors.FAIL + s + ShellColors.ENDC


def yellow(s: str) -> str:
    return ShellColors.WARNING + s + ShellColors.ENDC


# https://gist.github.com/egmontkob/eb114294efbcd5adb1944c9f3cb5feda
def link(location: str, text: str) -> str:
    """
    Emits a link to the terminal using the terminal hyperlink specification.

    Does not properly implement file URIs. Only use with web URIs.
    """
    return f"\033]8;;{location}\033\\{text}\033]8;;\033\\"


def error(msg: str) -> None:
    print(red("\nERROR: ") + msg)


def warning(msg: str) -> None:
    print(yellow("\nWARNING: ") + msg)


def log(msg: str, *, level: int = 1) -> None:
    """Log a message based on verbosity level.

    Args:
        msg: Message to log
        level: 1 for essential messages (always shown), 2+ for verbose-only messages
    """
    if level == 1 or (level >= 2 and is_verbose()):  # noqa: PLR2004
        print(msg)


# ===----------------------------------------------------------------------=== #
# Common utility functions
# ===----------------------------------------------------------------------=== #
def split_header(s: str) -> list[CommitHeader]:
    return [CommitHeader(h) for h in s.split("\0")[:-1]]


def last(ref: str, sep: str = "/") -> str:
    return ref.rsplit(sep, 1)[-1]


# TODO: Move to 'modular.utils.git'
def is_ancestor(commit1: str, commit2: str, *, verbose: bool) -> bool:
    """
    Returns true if 'commit1' is an ancestor of 'commit2'.
    """
    # TODO: We need to check returncode of this command more carefully, as the
    # command simply might fail (rc != 0 and rc != 1).
    p = run_shell_command(
        ["git", "merge-base", "--is-ancestor", commit1, commit2],
        check=False,
        quiet=not verbose,
    )
    return p.returncode == 0


def is_repo_clean() -> bool:
    """
    Returns true if there are no uncommitted changes in the repo.
    """
    changes = get_uncommitted_changes()
    changes.pop("??", [])  # We don't care about untracked files
    return not bool(changes)


def get_stack(base: str, head: str, *, verbose: bool) -> list[StackEntry]:
    if not is_ancestor(base, head, verbose=verbose):
        error(
            f"{base} is not an ancestor of {head}.\n"
            "Could not find commits for the stack."
        )
        sys.exit(1)

    # Find list of commits since merge base.
    st: list[StackEntry] = []
    stack = (
        split_header(
            get_command_output(["git", "rev-list", "--header", "^" + base, head])
        )
    )[::-1]

    for i in range(len(stack)):
        entry = StackEntry(stack[i])
        st.append(entry)

    for e in st:
        e.read_metadata()
    return st


def set_base_branches(st: list[StackEntry], target: str) -> None:
    prev_branch: str | None = target
    for e in st:
        e.base, prev_branch = prev_branch, e.head


def verify(st: list[StackEntry], *, check_base: bool = False) -> None:
    log(h("Verifying stack info"), level=2)
    for index, e in enumerate(st):
        if e.has_missing_info():
            error(ERROR_STACKINFO_MISSING.format(**locals()))
            raise RuntimeError

        if len(e.pr.split("/")) == 0 or not last(e.pr).isnumeric():
            error(ERROR_STACKINFO_BAD_LINK.format(**locals()))
            raise RuntimeError

        ghinfo = get_command_output(
            [
                "gh",
                "pr",
                "view",
                e.pr,
                "--json",
                "baseRefName,headRefName,number,state,body,title,url,mergeStateStatus",
            ]
        )
        d = json.loads(ghinfo)
        for required_field in ["state", "number", "baseRefName", "headRefName"]:
            if required_field not in d:
                error(ERROR_STACKINFO_MALFORMED_RESPONSE.format(**locals()))
                raise RuntimeError

        if d["state"] != "OPEN":
            error(ERROR_STACKINFO_PR_NOT_OPEN.format(**locals()))
            raise RuntimeError

        if int(last(e.pr)) != d["number"]:
            error(ERROR_STACKINFO_PR_NUMBER_MISMATCH.format(**locals()))
            raise RuntimeError

        if e.head != d["headRefName"]:
            error(ERROR_STACKINFO_PR_HEAD_MISMATCH.format(**locals()))
            raise RuntimeError

        # 'Base' branch might diverge when the stack is modified (e.g. when a
        # new commit is added to the middle of the stack). It is not an issue
        # if we're updating the stack (i.e. in 'submit'), but it is an issue if
        # we are trying to land it.
        if check_base and e.base != d["baseRefName"]:
            error(ERROR_STACKINFO_PR_BASE_MISMATCH.format(**locals()))
            raise RuntimeError

        # The first entry on the stack needs to be actually mergeable on GitHub.
        if (
            check_base
            and index == 0
            and d["mergeStateStatus"] not in ["CLEAN", "UNKNOWN", "UNSTABLE"]
        ):
            error(ERROR_STACKINFO_PR_NOT_MERGEABLE.format(**locals()))
            raise RuntimeError


def print_stack(st: list[StackEntry], *, links: bool, level: int = 1) -> None:
    log(b("Stack:"), level=level)
    for e in reversed(st):
        log("   * " + e.pprint(links=links), level=level)


def draft_bitmask_type(value: str) -> list[bool]:
    # Validate that only 0s and 1s are present
    if value and not set(value).issubset({"0", "1"}):
        raise argparse.ArgumentTypeError("Bitmask must only contain 0s and 1s.")

    # Convert to list of booleans
    return [bool(int(bit)) for bit in value]


# ===----------------------------------------------------------------------=== #
# SUBMIT
# ===----------------------------------------------------------------------=== #
def format_stack_info(pr: str, branch: str) -> str:
    """Format the stack-info metadata trailer for a commit message."""
    return f"stack-info: PR: {pr}, branch: {branch}"


def add_or_update_metadata(e: StackEntry, *, needs_rebase: bool, verbose: bool) -> bool:
    if needs_rebase:
        if not e.has_base() or not e.has_head():
            error("Stack entry has no base or head branch")
            raise RuntimeError

        run_shell_command(
            [
                "git",
                "rebase",
                e.base or "",
                e.head or "",
                "--committer-date-is-author-date",
            ],
            quiet=not verbose,
        )
    else:
        if not e.has_head():
            error("Stack entry has no head branch")
            raise RuntimeError

        run_shell_command(["git", "checkout", e.head], quiet=not verbose)

    commit_msg = e.commit.commit_msg()
    found_metadata = RE_STACK_INFO_LINE.search(commit_msg)
    if found_metadata:
        # Metadata is already there, skip this commit
        return needs_rebase

    # Add the stack info metadata to the commit message
    commit_msg += "\n\n" + format_stack_info(e.pr, e.head)
    run_shell_command(
        ["git", "commit", "--amend", "-F", "-"],
        input=commit_msg.encode(),
        quiet=not verbose,
    )
    return True


def fix_branch_name_template(branch_name_template: str) -> str:
    if "$ID" not in branch_name_template:
        return f"{branch_name_template}/$ID"

    return branch_name_template


@cache
def get_branch_name_base(branch_name_template: str) -> str:
    username = get_gh_username()
    current_branch_name = get_current_branch_name()
    branch_name_base = branch_name_template.replace("$USERNAME", username)
    return branch_name_base.replace("$BRANCH", current_branch_name)


def get_branch_id(branch_name_template: str, branch_name: str) -> str | None:
    branch_name_base = get_branch_name_base(branch_name_template)
    pattern = branch_name_base.replace(r"$ID", r"(\d+)")
    match = re.search(pattern, branch_name)
    if match:
        return match.group(1)
    return None


def generate_branch_name(branch_name_template: str, branch_id: int) -> str:
    branch_name_base = get_branch_name_base(branch_name_template)
    return branch_name_base.replace(r"$ID", str(branch_id))


def get_taken_branch_ids(refs: list[str], branch_name_template: str) -> list[int]:
    branch_ids = [get_branch_id(branch_name_template, ref) for ref in refs]
    return [int(branch_id) for branch_id in branch_ids if branch_id is not None]


def generate_available_branch_name(refs: list[str], branch_name_template: str) -> str:
    branch_ids = get_taken_branch_ids(refs, branch_name_template)
    max_ref_num = max(branch_ids) if branch_ids else 0
    new_branch_id = max_ref_num + 1
    return generate_branch_name(branch_name_template, new_branch_id)


def get_available_branch_name(remote: str, branch_name_template: str) -> str:
    branch_name_base = get_branch_name_base(branch_name_template)

    git_command_branch_template = branch_name_base.replace(r"$ID", "*")
    refs = get_command_output(
        [
            "git",
            "for-each-ref",
            f"refs/remotes/{remote}/{git_command_branch_template}",
            "--format='%(refname)'",
        ]
    ).split()

    refs = [ref.strip("'") for ref in refs]
    return generate_available_branch_name(refs, branch_name_template)


def get_next_available_branch_name(branch_name_template: str, name: str) -> str:
    branch_id = get_branch_id(branch_name_template, name)
    return generate_branch_name(branch_name_template, int(branch_id or 0) + 1)


def set_head_branches(
    st: list[StackEntry], remote: str, *, verbose: bool, branch_name_template: str
) -> None:
    """Set the head ref for each stack entry if it doesn't already have one."""

    run_shell_command(["git", "fetch", "--prune", remote], quiet=not verbose)
    available_name = get_available_branch_name(remote, branch_name_template)
    for e in filter(lambda e: not e.has_head(), st):
        e.head = available_name
        available_name = get_next_available_branch_name(
            branch_name_template, available_name
        )


def init_local_branches(
    st: list[StackEntry], remote: str, *, verbose: bool, branch_name_template: str
) -> None:
    log(h("Initializing local branches"), level=2)
    set_head_branches(
        st, remote, verbose=verbose, branch_name_template=branch_name_template
    )
    for e in st:
        run_shell_command(
            ["git", "checkout", e.commit.commit_id(), "-B", e.head],
            quiet=not verbose,
        )


RE_STALE_LEASE = re.compile(r"!\s+\[rejected\]\s+(\S+)\s+->\s+\S+\s+\(stale info\)")


def stale_lease_branches(stderr: str) -> list[str]:
    """Branch names rejected by --force-with-lease as stale, parsed from git's
    stderr (lines like `! [rejected] foo -> foo (stale info)`)."""
    return RE_STALE_LEASE.findall(stderr)


def force_push_with_lease(
    refspecs: list[str], remote: str, target: str, *, verbose: bool
) -> None:
    """Force-push *refspecs* with --force-with-lease.

    Using a lease means a PR branch that changed on the remote out-of-band (for
    example, a "Commit suggestion" accepted during review) is rejected rather
    than silently overwritten. The lease's expected value is the remote-tracking
    ref, which reflects what stack-pr last pushed (stack-pr does not fetch these
    branches between runs), so any upstream change since then trips it.

    On a stale-lease rejection, aborts with reconciliation guidance. New
    branches (no tracking ref) push fine. `--atomic` keeps a rejection from
    leaving a partially-updated stack.
    """
    cmd = ["git", "push", "--force-with-lease", "--atomic", remote, *refspecs]
    result = run_shell_command(cmd, quiet=not verbose, check=False, stderr=PIPE)
    if result.returncode == 0:
        return

    stderr = (result.stderr or b"").decode("utf-8", errors="replace")
    stale = stale_lease_branches(stderr)
    if stale or "stale info" in stderr:
        branches = ", ".join(stale) if stale else "one or more PR branches"
        error(ERROR_STALE_REMOTE_BRANCHES.format(
            branches=branches, remote=remote, target=target
        ))
        sys.exit(1)

    sys.stderr.write(stderr)
    raise SubprocessError(f"Failed to push branches (exit code {result.returncode}).")


def push_branches(
    st: list[StackEntry], remote: str, target: str, *, verbose: bool
) -> None:
    log(h("Updating remote branches"), level=2)
    force_push_with_lease(
        [f"{e.head}:{e.head}" for e in st], remote, target, verbose=verbose
    )


def print_cmd_failure_details(exc: SubprocessError) -> None:
    # Test if SubprocessError subclass has stdout and stderr attributes
    if hasattr(exc, "stdout") and exc.stdout:
        cmd_stdout = (
            exc.stdout.decode("utf-8").replace("\\n", "\n").replace("\\t", "\t")
        )
    else:
        cmd_stdout = None

    if hasattr(exc, "stderr") and exc.stderr:
        cmd_stderr = (
            exc.stderr.decode("utf-8").replace("\\n", "\n").replace("\\t", "\t")
        )
    else:
        cmd_stderr = None

    print(f"Exitcode: {exc.returncode if hasattr(exc, 'returncode') else 'unknown'}")
    print(f"Stdout: {cmd_stdout}")
    print(f"Stderr: {cmd_stderr}")


def create_pr(e: StackEntry, *, is_draft: bool, reviewer: str = "") -> None:
    # Don't do anything if the PR already exists
    if e.has_pr():
        return
    if not e.has_base() or not e.has_head():
        error("Stack entry has no base or head branch")
        raise RuntimeError
    log(h("Creating PR " + green(f"'{e.head}' -> '{e.base}'")), level=1)
    cmd = [
        "gh",
        "pr",
        "create",
        "-B",
        e.base or "",
        "-H",
        e.head or "",
        "-t",
        e.commit.title(),
        "-F",
        "-",
    ]
    if reviewer:
        cmd.extend(["--reviewer", reviewer])
    if is_draft:
        cmd.append("--draft")

    try:
        r = get_command_output(cmd, input=e.commit.commit_msg().encode())
    except Exception:
        error(ERROR_CANT_CREATE_PR.format(**locals()))
        raise

    log(b("Created: ") + r, level=2)
    e.pr = r.split()[-1]


def extract_toc_pr_ids(body: str) -> list[str]:
    """Extract the PR ids recorded in a PR body's cross-links table.

    Returns the ids in bottom-to-top stack order (the table is rendered
    top-first, so the result is reversed). Returns an empty list if the body
    doesn't contain a recognizable cross-links table.
    """
    if CROSS_LINKS_DELIMETER not in body:
        return []
    header = body.split(CROSS_LINKS_DELIMETER, 1)[0]
    if STACK_TOC_HEADER not in header:
        return []
    # findall yields ids top-first (as rendered); reverse to bottom-first.
    return RE_TOC_ENTRY.findall(header)[::-1]


def get_pr_state(pr_id: str) -> str:
    """Return the GitHub state of a PR: 'OPEN', 'MERGED', or 'CLOSED'."""
    out = get_command_output(["gh", "pr", "view", pr_id, "--json", "state"])
    return str(json.loads(out)["state"])


def build_stack_pr_list(st: list[StackEntry]) -> list[str]:
    """Build the ordered list of PR ids to show in every PR's cross-links.

    The list is maintained across submits: PRs that have left the active stack
    but were previously part of it (i.e. they merged or were closed) are kept,
    pinned below the active stack in their original order. PRs that left the
    stack but are still open are dropped. The result is bottom-to-top order.
    """
    active_ids = [last(e.pr) for e in st]
    active_set = set(active_ids)

    # Recover the previously recorded list from a surviving PR's body. Every PR
    # carries the full list, so the first one with a recognizable table wins.
    historical: list[str] = []
    for e in st:
        historical = extract_toc_pr_ids(get_pr_body(e))
        if historical:
            break

    # Keep historical entries that have left the stack but aren't open anymore.
    inactive: list[str] = []
    for pr_id in historical:
        if pr_id in active_set or pr_id in inactive:
            continue
        if get_pr_state(pr_id) != "OPEN":
            inactive.append(pr_id)

    return inactive + active_ids


def generate_toc(pr_ids: list[str], current: str) -> str:
    # Don't generate a table of contents for a lone PR with no history.
    if len(pr_ids) <= 1:
        return ""

    def toc_entry(pr_id: str) -> str:
        arrow = "__->__" if pr_id == current else ""
        return f" * {arrow}#{pr_id}\n"

    entries = (toc_entry(pr_id) for pr_id in pr_ids[::-1])
    return f"{STACK_TOC_HEADER}\n{''.join(entries)}\n"


def get_pr_body(e: StackEntry) -> str:
    out = get_command_output(
        ["gh", "pr", "view", e.pr, "--json", "body"],
    )
    return str(json.loads(out)["body"] or "").strip()


def edit_pr_base(
    pr: str,
    base: str,
    *,
    extra_args: list[str] | None = None,
    verbose: bool,
    **kwargs: object,
) -> None:
    """Run ``gh pr edit <pr> -B <base> [extra_args]``, tolerating merge queues.

    GitHub refuses to change the base branch of a PR that has been added to a
    merge queue (``Cannot change the base branch because the branch has been
    added to a merge queue``). Such a PR is already on its way to landing, so
    instead of crashing we warn and retry the edit without the ``-B`` flag, so
    any other updates (``extra_args``, e.g. title/body) still get applied.
    """
    extra_args = extra_args or []
    result = run_shell_command(
        ["gh", "pr", "edit", pr, "-B", base, *extra_args],
        quiet=not verbose,
        check=False,
        stderr=PIPE,
        **kwargs,
    )
    if result.returncode == 0:
        return

    stderr = (result.stderr or b"").decode("utf-8", errors="replace")
    if "merge queue" not in stderr:
        sys.stderr.write(stderr)
        raise SubprocessError(f"Failed to edit {pr} (exit code {result.returncode}).")

    warning(
        f"Could not change the base branch of {pr}: it has been added to a "
        "merge queue. Leaving its base branch unchanged."
    )
    if extra_args:
        # Re-run without the base change so the remaining edits still apply.
        run_shell_command(
            ["gh", "pr", "edit", pr, *extra_args], quiet=not verbose, **kwargs
        )


def add_cross_links(st: list[StackEntry], *, keep_body: bool, verbose: bool) -> None:
    # Build the maintained list of PRs once - it's identical for every PR in
    # the stack (apart from which one is marked as current).
    pr_ids = build_stack_pr_list(st)

    for e in st:
        pr_id = last(e.pr)
        pr_toc = generate_toc(pr_ids, pr_id)

        title = e.commit.title()
        body = e.commit.commit_msg()

        # Strip title from the body - we will print it separately.
        body = "\n".join(body.splitlines()[1:])

        # Strip stack-info from the body, nothing interesting there.
        body = RE_STACK_INFO_LINE.sub("", body)

        # Build PR body components
        header = []
        body_content = body

        if pr_toc:
            # Multi-PR stack: add TOC header and format body with title
            header = [pr_toc, f"{CROSS_LINKS_DELIMETER}\n"]
            body_content = f"### {title}\n\n{body}"

        if keep_body:
            # Keep current body of the PR after the cross links component
            current_pr_body = get_pr_body(e)
            body_content = current_pr_body.split(CROSS_LINKS_DELIMETER, 1)[-1].lstrip()

        pr_body = [*header, body_content]

        if e.has_base():
            edit_pr_base(
                e.pr,
                e.base or "",
                extra_args=["-t", title, "-F", "-"],
                verbose=verbose,
                input="\n".join(pr_body).encode(),
            )
        else:
            error("Stack entry has no base branch")
            raise RuntimeError


# Temporarily set base branches of existing PRs to the bottom of the stack.
# This needs to be done to avoid PRs getting closed when commits are
# rearranged.
#
# For instance, if we first had
#
# Stack:
#    * #2 (stack/2 -> stack/1)  aaaa
#    * #1 (stack/1 -> main)     bbbb
#
# And then swapped the order of the commits locally and tried submitting again
# we would have:
#
# Stack:
#    * #1 (stack/1 -> main)     bbbb
#    * #2 (stack/2 -> stack/1)  aaaa
#
# Now we need to 1) change bases of the PRs, 2) push branches stack/1 and
# stack/2. If we push stack/1, then PR #2 gets automatically closed, since its
# head branch will contain all the commits from its base branch.
#
# To avoid this, we temporarily set all base branches to point to 'main'.
#
# We intentionally do NOT touch the draft/ready status of existing PRs here:
# a PR's review state is owned by the user, so re-running 'submit' must never
# flip a ready PR back to draft (or vice versa).
def reset_remote_base_branches(
    st: list[StackEntry], target: str, *, verbose: bool
) -> None:
    log(h("Resetting remote base branches"), level=2)

    for e in filter(lambda e: e.has_pr(), st):
        edit_pr_base(e.pr, target, verbose=verbose)


# If local 'main' lags behind 'origin/main', and 'head' contains all commits
# from 'main' to 'origin/main', then we can just move 'main' forward.
#
# It is a common user mistake to not update their local branch, run 'submit',
# and end up with a huge stack of changes that are already merged.
# We could've told users to update their local branch in that scenario, but why
# not to do it for them?
# In the very unlikely case when they indeed wanted to include changes that are
# already in remote into their stack, they can use a different notation for the
# base (e.g. explicit hash of the commit) - but most probably nobody ever would
# need that.
def should_update_local_base(
    head: str, base: str, remote: str, target: str, *, verbose: bool
) -> bool:
    base_hash = get_command_output(["git", "rev-parse", base])
    target_hash = get_command_output(["git", "rev-parse", f"{remote}/{target}"])
    return (
        is_ancestor(base, f"{remote}/{target}", verbose=verbose)
        and is_ancestor(f"{remote}/{target}", head, verbose=verbose)
        and base_hash != target_hash
    )


def update_local_base(base: str, remote: str, target: str, *, verbose: bool) -> None:
    log(h(f"Updating local branch {base} to {remote}/{target}"), level=1)
    run_shell_command(["git", "rebase", f"{remote}/{target}", base], quiet=not verbose)


@dataclass
class CommonArgs:
    """Class to help type checkers and separate implementation for CLI args."""

    base: str
    head: str
    remote: str
    target: str
    hyperlinks: bool
    verbose: bool
    branch_name_template: str
    show_tips: bool
    land_disabled: bool

    @classmethod
    def from_args(cls, args: argparse.Namespace, *, land_disabled: bool) -> CommonArgs:
        return cls(
            args.base,
            args.head,
            args.remote,
            args.target,
            args.hyperlinks,
            args.verbose,
            args.branch_name_template,
            args.show_tips,
            land_disabled,
        )


def check_target_branch_exists(args: CommonArgs) -> None:
    """Check that the target branch exists on the remote.

    Args:
        args: CommonArgs containing remote and target branch information

    Raises:
        SystemExit: If the target branch doesn't exist
    """
    # Check if target branch exists using git rev-parse --verify
    # This is fast and doesn't require listing all branches
    result = run_shell_command(
        ["git", "rev-parse", "--verify", f"{args.remote}/{args.target}"],
        quiet=True,
        check=False,
    )

    if result.returncode == 0:
        # Target branch exists, all good
        return

    # Target branch doesn't exist
    # Check if this is the common case where repo uses 'master' instead of 'main'
    if args.target == "main":
        master_result = run_shell_command(
            ["git", "rev-parse", "--verify", f"{args.remote}/master"],
            quiet=True,
            check=False,
        )
        if master_result.returncode == 0:
            # Master exists, show helpful error
            error(
                ERROR_TARGET_BRANCH_MASTER_INSTEAD_OF_MAIN.format(
                    remote=args.remote,
                    target=args.target,
                )
            )
            sys.exit(1)

    # Generic error for other cases
    error(
        ERROR_TARGET_BRANCH_MISSING.format(
            remote=args.remote,
            target=args.target,
        )
    )
    sys.exit(1)


def deduce_base(args: CommonArgs) -> CommonArgs:
    """Deduce the base branch from the head and target branches.

    If the base isn't explicitly specified, find the merge base between
    'origin/main' and 'head'.

    E.g. in the example below we want to include commits E and F into the stack,
    and to do that we pick B as our base:

    --> a ----> b  ----> c ----> d
    (main)       \\         (origin/main)
                  \\
                    ---> e ----> f
                            (head)
   """
    if args.base:
        return args
    deduced_base = get_command_output(
        ["git", "merge-base", args.head, f"{args.remote}/{args.target}"]
    )
    return CommonArgs(
        deduced_base,
        args.head,
        args.remote,
        args.target,
        args.hyperlinks,
        args.verbose,
        args.branch_name_template,
        args.show_tips,
        args.land_disabled,
    )


def print_tips_after_export(st: list[StackEntry], args: CommonArgs) -> None:
    stack_size = len(st)
    if stack_size == 0 or not args.show_tips:
        return

    top_commit = args.head
    if top_commit == "HEAD":
        top_commit = get_current_branch_name()

    log(b("\nOnce the stack is reviewed, it is ready to land!"), level=1)
    if not args.land_disabled:
        log(LAND_STACK_TIP.format(**locals()))


# ===----------------------------------------------------------------------=== #
# Entry point for 'submit' command
# ===----------------------------------------------------------------------=== #
def command_submit(
    args: CommonArgs,
    *,
    draft: bool,
    reviewer: str,
    keep_body: bool,
    draft_bitmask: list[bool] | None = None,
) -> None:
    """Entry point for 'submit' command.

    Args:
        args: CommonArgs object containing command line arguments.
        draft: Boolean flag indicating if the PRs should be created as drafts.
        reviewer: String representing the reviewer of the PRs.
        keep_body: Boolean flag indicating if the body of the PRs should be kept.
        draft_bitmask: List of boolean values indicating if each PR should be created as
            a draft.
    """
    log(h("SUBMIT"), level=1)

    if is_rebase_in_progress():
        error(ERROR_REBASE_IN_PROGRESS)
        sys.exit(1)

    current_branch = get_current_branch_name()

    if should_update_local_base(
        head=args.head,
        base=args.base,
        remote=args.remote,
        target=args.target,
        verbose=args.verbose,
    ):
        update_local_base(
            base=args.base, remote=args.remote, target=args.target, verbose=args.verbose
        )
        run_shell_command(["git", "checkout", current_branch], quiet=not args.verbose)

    # Determine what commits belong to the stack
    st = get_stack(base=args.base, head=args.head, verbose=args.verbose)
    if not st:
        log(h("Empty stack!"))
        log(h(blue("SUCCESS!")))
        return

    if (draft_bitmask is not None) and (len(draft_bitmask) != len(st)):
        log(h("Draft bitmask passed to 'submit' doesn't match number of PRs!"))
        return

    # Create local branches and initialize base and head fields in the stack
    # elements
    init_local_branches(
        st,
        args.remote,
        verbose=args.verbose,
        branch_name_template=args.branch_name_template,
    )
    set_base_branches(st, args.target)

    # If the current branch contains commits from the stack, we will need to
    # rebase it in the end since the commits will be modified.
    top_branch = st[-1].head
    need_to_rebase_current = is_ancestor(
        top_branch, current_branch, verbose=args.verbose
    )

    reset_remote_base_branches(st, target=args.target, verbose=args.verbose)

    # Push local branches to remote
    push_branches(st, remote=args.remote, target=args.target, verbose=args.verbose)

    # Now we have all the branches, so we can create the corresponding PRs
    log(h("Submitting PRs"), level=1)
    for e_idx, e in enumerate(st):
        is_pr_draft = draft or ((draft_bitmask is not None) and draft_bitmask[e_idx])
        create_pr(e, is_draft=is_pr_draft, reviewer=reviewer)

    # Verify consistency in everything we have so far
    verify(st)

    # Print stack now that PRs have been created
    print_stack(st, links=args.hyperlinks)

    # Embed stack-info into commit messages
    log(h("Updating commit messages with stack metadata"), level=2)
    needs_rebase = False
    for e in st:
        try:
            needs_rebase = add_or_update_metadata(
                e, needs_rebase=needs_rebase, verbose=args.verbose
            )
        except Exception:
            error(ERROR_CANT_UPDATE_META.format(**locals()))
            raise

    push_branches(st, remote=args.remote, target=args.target, verbose=args.verbose)

    log(h("Adding cross-links to PRs"), level=1)
    add_cross_links(st, keep_body=keep_body, verbose=args.verbose)

    if need_to_rebase_current:
        log(h(f"Rebasing the original branch '{current_branch}'"), level=2)
        run_shell_command(
            [
                "git",
                "rebase",
                top_branch,
                current_branch,
                "--committer-date-is-author-date",
            ],
            quiet=not args.verbose,
        )
    else:
        log(h(f"Checking out the original branch '{current_branch}'"), level=2)
        run_shell_command(["git", "checkout", current_branch], quiet=not args.verbose)

    delete_local_branches(st, verbose=args.verbose)
    print_tips_after_export(st, args)
    log(h(blue("SUCCESS!")), level=1)


# ===----------------------------------------------------------------------=== #
# LAND
# ===----------------------------------------------------------------------=== #
def rebase_pr(e: StackEntry, remote: str, target: str, *, verbose: bool) -> None:
    log(b("Rebasing ") + e.pprint(links=False), level=2)
    # Rebase the head branch to the most recent 'origin/main'
    run_shell_command(["git", "fetch", "--prune", remote], quiet=not verbose)
    cmd = ["git", "checkout", f"{remote}/{e.head}", "-B", e.head]
    try:
        run_shell_command(cmd, quiet=not verbose)
    except Exception:
        error(ERROR_CANT_CHECKOUT_REMOTE_BRANCH.format(**locals()))
        raise

    cmd = [
        "git",
        "rebase",
        f"{remote}/{target}",
        e.head,
        "--committer-date-is-author-date",
    ]
    try:
        run_shell_command(cmd, quiet=not verbose)
    except Exception:
        error(ERROR_CANT_REBASE.format(**locals()))
        raise
    force_push_with_lease([f"{e.head}:{e.head}"], remote, target, verbose=verbose)


def land_pr(e: StackEntry, remote: str, target: str, *, verbose: bool) -> None:
    log(b("Landing ") + e.pprint(links=False), level=2)
    # Rebase the head branch to the most recent 'origin/main'
    run_shell_command(["git", "fetch", "--prune", remote], quiet=not verbose)
    cmd = ["git", "checkout", f"{remote}/{e.head}", "-B", e.head]
    try:
        run_shell_command(cmd, quiet=not verbose)
    except Exception:
        error(ERROR_CANT_CHECKOUT_REMOTE_BRANCH.format(**locals()))
        raise

    # Switch PR base branch to 'main'
    run_shell_command(["gh", "pr", "edit", e.pr, "-B", target], quiet=not verbose)

    # Form the commit message: it should contain the original commit message
    # and nothing else.
    pr_body = RE_STACK_INFO_LINE.sub("", e.commit.commit_msg())

    # Since title is passed separately, we need to strip the first line from the
    # body:
    lines = pr_body.splitlines()
    pr_id = last(e.pr)
    title = f"{lines[0]} (#{pr_id})"
    pr_body = "\n".join(lines[1:]) or " "
    run_shell_command(
        ["gh", "pr", "merge", e.pr, "--squash", "-t", title, "-F", "-"],
        input=pr_body.encode(),
        quiet=not verbose,
    )


def delete_local_branches(st: list[StackEntry], *, verbose: bool) -> None:
    log(h("Deleting local branches"), level=2)
    # Delete local branches
    cmd = ["git", "branch", "-D"]
    cmd.extend([e.head for e in st if e.head])
    run_shell_command(cmd, check=False, quiet=not verbose)


def delete_remote_branches(
    st: list[StackEntry], remote: str, *, verbose: bool, branch_name_template: str
) -> None:
    log(h("Deleting remote branches"), level=1)
    run_shell_command(["git", "fetch", "--prune", remote], quiet=not verbose)

    branch_name_base = get_branch_name_base(branch_name_template)
    refs = get_command_output(
        [
            "git",
            "for-each-ref",
            f"refs/remotes/{remote}/{branch_name_base}",
            "--format=%(refname)",
        ]
    ).split()
    refs = [x.replace(f"refs/remotes/{remote}/", "") for x in refs]
    remote_branches_to_delete = [e.head for e in st if e.head in refs]

    if remote_branches_to_delete:
        cmd = ["git", "push", "-f", remote]
        cmd.extend([f":{branch}" for branch in remote_branches_to_delete])
        run_shell_command(cmd, check=False, quiet=not verbose)


# ===----------------------------------------------------------------------=== #
# Entry point for 'land' command
# ===----------------------------------------------------------------------=== #
def command_land(args: CommonArgs) -> None:
    log(h("LAND"), level=1)

    current_branch = get_current_branch_name()

    if should_update_local_base(
        head=args.head,
        base=args.base,
        remote=args.remote,
        target=args.target,
        verbose=args.verbose,
    ):
        update_local_base(
            base=args.base, remote=args.remote, target=args.target, verbose=args.verbose
        )
        run_shell_command(["git", "checkout", current_branch], quiet=not args.verbose)

    # Determine what commits belong to the stack
    st = get_stack(base=args.base, head=args.head, verbose=args.verbose)
    if not st:
        log(h("Empty stack!"), level=1)
        log(h(blue("SUCCESS!")), level=1)
        return

    # Initialize base branches of elements in the stack. Head branches should
    # already be there from the metadata that commits need to have by that
    # point.
    set_base_branches(st, args.target)
    print_stack(st, links=args.hyperlinks)

    # Verify that the stack is correct before trying to land it.
    verify(st, check_base=True)

    # All good, land the bottommost PR!
    land_pr(st[0], remote=args.remote, target=args.target, verbose=args.verbose)

    # The rest of the stack now needs to be rebased.
    if len(st) > 1:
        log(h("Rebasing the rest of the stack"), level=1)
        prs_to_rebase = st[1:]
        print_stack(prs_to_rebase, links=args.hyperlinks, level=1)
        for e in prs_to_rebase:
            rebase_pr(e, remote=args.remote, target=args.target, verbose=args.verbose)
        # Change the target of the new bottom-most PR in the stack to 'target'
        run_shell_command(
            ["gh", "pr", "edit", prs_to_rebase[0].pr, "-B", args.target],
            quiet=not args.verbose,
        )

    # Delete local and remote stack branches
    run_shell_command(["git", "checkout", current_branch], quiet=not args.verbose)

    delete_local_branches(st, verbose=args.verbose)

    # If local branch {target} exists, rebase it on the remote/target
    if branch_exists(args.target):
        run_shell_command(
            ["git", "rebase", f"{args.remote}/{args.target}", args.target],
            quiet=not args.verbose,
        )
    run_shell_command(
        ["git", "rebase", f"{args.remote}/{args.target}", current_branch],
        quiet=not args.verbose,
    )

    log(h(blue("SUCCESS!")))


# ===----------------------------------------------------------------------=== #
# ABANDON
# ===----------------------------------------------------------------------=== #
def strip_metadata(e: StackEntry, *, needs_rebase: bool, verbose: bool) -> str:
    """Strip the stack metadata from the commit message and amend the commit.

    Args:
        e: StackEntry object representing the commit to strip metadata from.
        needs_rebase: Boolean flag indicating if the commit needs to be rebased.
        verbose: Boolean flag indicating if verbose output should be printed.

    Returns:
        The SHA of the commit after stripping the metadata.
    """
    m = e.commit.commit_msg()

    m = RE_STACK_INFO_LINE.sub("", m)
    if needs_rebase:
        if not e.has_base() or not e.has_head():
            error("Stack entry has no base or head branch")
            raise RuntimeError
        run_shell_command(
            [
                "git",
                "rebase",
                e.base or "",
                e.head or "",
                "--committer-date-is-author-date",
            ],
            quiet=not verbose,
        )
    else:
        if not e.has_head():
            error("Stack entry has no head branch")
            raise RuntimeError
        run_shell_command(["git", "checkout", e.head or ""], quiet=not verbose)

    run_shell_command(
        ["git", "commit", "--amend", "-F", "-"],
        input=m.encode(),
        quiet=not verbose,
    )

    return get_command_output(["git", "rev-parse", e.head])


# ===----------------------------------------------------------------------=== #
# Entry point for 'abandon' command
# ===----------------------------------------------------------------------=== #
def command_abandon(args: CommonArgs) -> None:
    log(h("ABANDON"))
    st = get_stack(base=args.base, head=args.head, verbose=args.verbose)
    if not st:
        log(h("Empty stack!"))
        log(h(blue("SUCCESS!")))
        return
    current_branch = get_current_branch_name()

    init_local_branches(
        st,
        remote=args.remote,
        verbose=args.verbose,
        branch_name_template=args.branch_name_template,
    )
    set_base_branches(st, args.target)
    print_stack(st, links=args.hyperlinks)

    log(h("Stripping stack metadata from commit messages"))

    last_hash = ""
    # The first commit doesn't need to be rebased since its will not change.
    # The rest of the commits need to be rebased since their base will be
    # changed as we strip the metadata from the commit messages.
    need_rebase = False
    for e in st:
        last_hash = strip_metadata(e, needs_rebase=need_rebase, verbose=args.verbose)
        need_rebase = True

    log(h("Rebasing the current branch on top of updated top branch"))
    run_shell_command(
        ["git", "rebase", last_hash, current_branch], quiet=not args.verbose
    )

    delete_local_branches(st, verbose=args.verbose)
    delete_remote_branches(
        st,
        remote=args.remote,
        verbose=args.verbose,
        branch_name_template=args.branch_name_template,
    )
    log(h(blue("SUCCESS!")))


# ===----------------------------------------------------------------------=== #
# ADOPT
# ===----------------------------------------------------------------------=== #
def get_adopt_pr_info(pr: str | None) -> dict:
    """Look up the PR to adopt via 'gh'.

    With no explicit `pr`, this resolves the PR associated with the currently
    checked-out branch. Returns the parsed JSON object from 'gh pr view'.
    """
    cmd = ["gh", "pr", "view"]
    if pr:
        cmd.append(pr)
    cmd += ["--json", "number,headRefName,headRefOid,state,url"]
    try:
        out = get_command_output(cmd)
    except SubprocessError:
        error(ERROR_ADOPT_NO_PR.format(cmd=cmd))
        raise
    return json.loads(out)


def warn_if_content_differs(
    e: StackEntry, pr_info: dict, *, remote: str, verbose: bool
) -> None:
    """Warn if the local commit's tree differs from the adopted PR's head.

    'submit' will force-push the local commit to the PR's branch, so a mismatch
    means the PR's diff will change. This is informational only; it never blocks
    adoption, and silently does nothing if the comparison can't be made.
    """
    head_oid = pr_info.get("headRefOid")
    if not head_oid:
        return

    object_present = (
        run_shell_command(
            ["git", "cat-file", "-e", head_oid], quiet=True, check=False
        ).returncode
        == 0
    )
    if not object_present:
        run_shell_command(
            ["git", "fetch", remote, pr_info["headRefName"]],
            quiet=not verbose,
            check=False,
        )

    try:
        pr_tree = get_command_output(["git", "rev-parse", f"{head_oid}^{{tree}}"])
    except SubprocessError:
        # Couldn't resolve the PR head locally; skip the comparison.
        return

    if pr_tree != e.commit.tree():
        log(red(WARN_ADOPT_CONTENT_DIFFERS.format(pr=pr_info["url"])))


def adopt_commit(
    e: StackEntry, pr: str, branch: str, *, current_branch: str, verbose: bool
) -> None:
    """Embed stack-info metadata into a selected commit of the stack.

    The target commit may not be HEAD (commits can be stacked on top of it), so
    the message is rewritten on a detached checkout and the rest of the branch
    is replayed onto the rewritten commit.
    """
    old_sha = e.commit.commit_id()
    new_msg = e.commit.commit_msg() + "\n\n" + format_stack_info(pr, branch)

    run_shell_command(["git", "checkout", old_sha], quiet=not verbose)
    run_shell_command(
        ["git", "commit", "--amend", "-F", "-"],
        input=new_msg.encode(),
        quiet=not verbose,
    )
    new_sha = get_command_output(["git", "rev-parse", "HEAD"])

    # Replay any commits stacked on top of the target onto the rewritten commit.
    run_shell_command(
        [
            "git",
            "rebase",
            "--onto",
            new_sha,
            old_sha,
            current_branch,
            "--committer-date-is-author-date",
        ],
        quiet=not verbose,
    )


def print_tips_after_adopt(st: list[StackEntry], args: CommonArgs) -> None:
    if not st or not args.show_tips:
        return
    log(ADOPT_TIP)


def select_adopt_entry(st: list[StackEntry], commit: str | None) -> StackEntry:
    """Pick the stack entry to adopt the PR onto.

    Defaults to the bottom-most commit; if `commit` is given, resolves it and
    matches it against the entries in the stack.
    """
    if commit is None:
        # The bottom-most commit is the entry whose base is the target branch.
        return st[0]

    try:
        sha = get_command_output(["git", "rev-parse", commit])
    except SubprocessError:
        error(ERROR_ADOPT_BAD_COMMIT.format(commit=commit, cmd=["git", "rev-parse", commit]))
        raise

    for e in st:
        if e.commit.commit_id() == sha:
            return e

    error(ERROR_ADOPT_COMMIT_NOT_IN_STACK.format(commit=commit, sha=sha))
    sys.exit(1)


# ===----------------------------------------------------------------------=== #
# Entry point for 'adopt' command
# ===----------------------------------------------------------------------=== #
def command_adopt(args: CommonArgs, pr: str | None, commit: str | None) -> None:
    log(h("ADOPT"))

    st = get_stack(base=args.base, head=args.head, verbose=args.verbose)
    if not st:
        log(h("Empty stack!"))
        log(h(blue("SUCCESS!")))
        return

    # By default adopt applies to the bottom-most commit (the entry whose base
    # is the target branch); a specific commit can be targeted with --commit.
    e = select_adopt_entry(st, commit)
    if RE_STACK_INFO_LINE.search(e.commit.commit_msg()):
        error(ERROR_ADOPT_ALREADY_MANAGED.format(e=e))
        sys.exit(1)

    pr_info = get_adopt_pr_info(pr)
    state = pr_info.get("state")
    if state != "OPEN":
        error(ERROR_ADOPT_PR_NOT_OPEN.format(pr=pr_info.get("url", pr), state=state))
        sys.exit(1)

    pr_url = pr_info["url"]
    head_ref = pr_info["headRefName"]

    warn_if_content_differs(e, pr_info, remote=args.remote, verbose=args.verbose)

    current_branch = get_current_branch_name()
    log(
        h(
            f"Adopting PR {pr_url} (branch '{head_ref}') onto "
            + e.pprint(links=False)
        )
    )
    adopt_commit(
        e, pr_url, head_ref, current_branch=current_branch, verbose=args.verbose
    )

    # Re-read the stack to reflect the freshly embedded metadata and show it.
    run_shell_command(["git", "checkout", current_branch], quiet=not args.verbose)
    st = get_stack(base=args.base, head=args.head, verbose=args.verbose)
    set_head_branches(
        st,
        remote=args.remote,
        verbose=args.verbose,
        branch_name_template=args.branch_name_template,
    )
    set_base_branches(st, target=args.target)
    print_stack(st, links=args.hyperlinks)
    print_tips_after_adopt(st, args)
    log(h(blue("SUCCESS!")))


# ===----------------------------------------------------------------------=== #
# VIEW
# ===----------------------------------------------------------------------=== #
def print_tips_after_view(st: list[StackEntry], args: CommonArgs) -> None:
    stack_size = len(st)
    if stack_size == 0 or not args.show_tips:
        return

    ready_to_land = all(not e.has_missing_info() for e in st)

    top_commit = args.head
    if top_commit == "HEAD":
        top_commit = get_current_branch_name()

    if ready_to_land:
        log(b("\nThis stack is ready to land!"))
        log(UPDATE_STACK_TIP.format(**locals()))
        if not args.land_disabled:
            log(LAND_STACK_TIP.format(**locals()))
        return

    # Stack is not ready to land, suggest exporting it first
    log(b("\nThis stack can't be landed yet, you need to export it first."))
    log(EXPORT_STACK_TIP.format(**locals()))


# ===----------------------------------------------------------------------=== #
# Entry point for 'view' command
# ===----------------------------------------------------------------------=== #
def command_view(args: CommonArgs) -> None:
    log(h("VIEW"))

    if should_update_local_base(
        head=args.head,
        base=args.base,
        remote=args.remote,
        target=args.target,
        verbose=args.verbose,
    ):
        log(
            red(
                f"\nWarning: Local '{args.base}' is behind"
                f" '{args.remote}/{args.target}'!"
            ),
        )
        log(
            ("Consider updating your local branch by running the following commands:"),
        )
        log(
            b(f"   git rebase {args.remote}/{args.target} {args.base}"),
        )
        log(
            b(f"   git checkout {get_current_branch_name()}\n"),
        )

    st = get_stack(base=args.base, head=args.head, verbose=args.verbose)

    set_head_branches(
        st,
        remote=args.remote,
        verbose=args.verbose,
        branch_name_template=args.branch_name_template,
    )
    set_base_branches(st, target=args.target)
    print_stack(st, links=args.hyperlinks)
    print_tips_after_view(st, args)
    log(h(blue("SUCCESS!")))


# ===----------------------------------------------------------------------=== #
# CONFIG
# ===----------------------------------------------------------------------=== #
def command_config(config_file: str, setting: str) -> None:
    """Set a configuration value in the config file.

    Args:
        config_file: Path to the config file
        setting: Setting in the format "section.key=value"
    """
    if "=" not in setting:
        error(ERROR_CONFIG_INVALID_FORMAT)
        sys.exit(1)

    key_path, value = setting.split("=", 1)

    if "." not in key_path:
        error(ERROR_CONFIG_INVALID_FORMAT)
        sys.exit(1)

    section, key = key_path.split(".", 1)

    config = configparser.ConfigParser()
    if Path(config_file).is_file():
        config.read(config_file)

    if not config.has_section(section):
        config.add_section(section)

    config.set(section, key, value)

    with Path(config_file).open("w") as f:
        config.write(f)

    print(f"Set {section}.{key} = {value}")


def command_install(name: str, *, local: bool) -> None:
    """Install stack-pr as a git alias so it can be run as `git <name>`.

    Writes e.g. ``[alias] stack = !stack-pr`` to the user's git config.
    """
    scope = "--local" if local else "--global"
    alias_value = "!stack-pr"
    run_shell_command(
        ["git", "config", scope, f"alias.{name}", alias_value],
        quiet=True,
    )
    print(f"Installed git alias '{name}' -> '{alias_value}' ({scope}).")
    print("\nYou can now invoke stack-pr through git, e.g.:")
    print(f"  git {name} view")
    print(f"  git {name} submit")
    print(
        f"  git {name} help     # note: 'git {name} --help' is intercepted by git,"
    )
    print(f"                      # so use 'git {name} help' instead.")


def command_help(parser: argparse.ArgumentParser, topic: str | None) -> None:
    """Print help. With a *topic*, show that subcommand's help.

    Exists so `git stack help [cmd]` works: git intercepts `git stack --help`
    for aliases and only prints "'stack' is aliased to '!stack-pr'".
    """
    if topic:
        # Defer to argparse's own per-subcommand help (this exits the process).
        parser.parse_args([topic, "--help"])
    else:
        parser.print_help()


# ===----------------------------------------------------------------------=== #
# Main entry point
# ===----------------------------------------------------------------------=== #


def create_argparser(
    config: configparser.ConfigParser,
) -> argparse.ArgumentParser:
    """Helper for CL option definition and parsing logic."""
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(help="sub-command help", dest="command")

    common_parser = argparse.ArgumentParser(add_help=False)
    common_parser.add_argument(
        "-R",
        "--remote",
        default=config.get("repo", "remote", fallback="origin"),
        help="Remote name",
    )
    common_parser.add_argument("-B", "--base", help="Local base branch")
    common_parser.add_argument("-H", "--head", default="HEAD", help="Local head branch")
    common_parser.add_argument(
        "-T",
        "--target",
        default=config.get("repo", "target", fallback="main"),
        help="Remote target branch",
    )
    common_parser.add_argument(
        "--hyperlinks",
        action=argparse.BooleanOptionalAction,
        default=config.getboolean("common", "hyperlinks", fallback=True),
        help="Enable or disable hyperlink support.",
    )
    common_parser.add_argument(
        "-V",
        "--verbose",
        action="store_true",
        default=config.getboolean("common", "verbose", fallback=False),
        help="Enable verbose output from Git subcommands.",
    )
    common_parser.add_argument(
        "--branch-name-template",
        default=config.get("repo", "branch_name_template", fallback="$USERNAME/stack"),
        help="A template for names of the branches stack-pr would use.",
    )
    common_parser.add_argument(
        "--show-tips",
        action=argparse.BooleanOptionalAction,
        default=config.getboolean("common", "show_tips", fallback=True),
        help="Show or hide usage tips after commands.",
    )

    parser_submit = subparsers.add_parser(
        "submit",
        aliases=["export"],
        help="Submit a stack of PRs",
        parents=[common_parser],
    )
    parser_submit.add_argument(
        "--keep-body",
        action="store_true",
        default=config.getboolean("common", "keep_body", fallback=False),
        help="Keep current PR body and only add/update cross links",
    )
    parser_submit.add_argument(
        "-d",
        "--draft",
        action="store_true",
        default=config.getboolean("common", "draft", fallback=False),
        help="Submit PRs in draft mode",
    )
    parser_submit.add_argument(
        "--draft-bitmask",
        type=draft_bitmask_type,
        default=None,
        help="Bitmask of whether each PR is a draft (optional).",
    )
    parser_submit.add_argument(
        "--reviewer",
        default=os.getenv(
            "STACK_PR_DEFAULT_REVIEWER",
            default=config.get("repo", "reviewer", fallback=""),
        ),
        help="List of reviewers for the PR",
    )
    parser_submit.add_argument(
        "-s",
        "--stash",
        action="store_true",
        default=config.getboolean("common", "stash", fallback=False),
        help="Stash all uncommited changes before submitting the PR",
    )

    land_style = config.get("land", "style", fallback="bottom-only")
    if land_style == "bottom-only":
        subparsers.add_parser(
            "land",
            help="Land the bottom-most PR in the current stack",
            parents=[common_parser],
        )
    subparsers.add_parser(
        "abandon",
        help="Abandon the current stack",
        parents=[common_parser],
    )
    parser_adopt = subparsers.add_parser(
        "adopt",
        help="Bring an existing PR under stack-pr management",
        parents=[common_parser],
    )
    parser_adopt.add_argument(
        "pr",
        nargs="?",
        default=None,
        help=(
            "PR number or URL to adopt. If omitted, the PR of the current "
            "branch is used."
        ),
    )
    parser_adopt.add_argument(
        "--commit",
        default=None,
        help=(
            "Commit to attach the PR to (any git revision). If omitted, the "
            "bottom-most commit of the stack is used."
        ),
    )
    subparsers.add_parser(
        "view",
        help="Inspect the current stack",
        parents=[common_parser],
    )

    # autoland lives in its own module to keep this file small.
    from stack_pr import autoland  # noqa: PLC0415

    autoland.register_parser(subparsers, common_parser)

    parser_config = subparsers.add_parser(
        "config",
        help="Set a configuration value",
    )
    parser_config.add_argument(
        "setting",
        help="Configuration setting in format <section>.<key>=<value>",
    )

    parser_install = subparsers.add_parser(
        "install",
        help="Install stack-pr as a git alias (e.g. so `git stack` works)",
    )
    parser_install.add_argument(
        "--name",
        default="stack",
        help="Git alias name to create (default: stack, i.e. `git stack ...`).",
    )
    parser_install.add_argument(
        "--local",
        action="store_true",
        help="Write to the repository's git config instead of the global one.",
    )

    parser_help = subparsers.add_parser(
        "help",
        help="Show help (use `git stack help`; `git stack --help` is intercepted by git)",
    )
    parser_help.add_argument(
        "topic",
        nargs="?",
        default=None,
        help="Optional subcommand to show help for (e.g. `help submit`).",
    )

    return parser


def load_config(config_file: str | Path) -> configparser.ConfigParser:
    config = configparser.ConfigParser()
    if Path(config_file).is_file():
        config.read(config_file)
    return config


def main() -> None:  # noqa: PLR0912, PLR0915, C901
    repo_config_file = get_repo_root() / ".stack-pr.cfg"
    config_file = os.getenv("STACKPR_CONFIG", repo_config_file)
    config = load_config(config_file)

    parser = create_argparser(config)
    args = parser.parse_args()

    # Set global verbose flag (if present - config command doesn't have it)
    if hasattr(args, "verbose"):
        set_verbose(args.verbose)

    if not args.command:
        print(h(red("Invalid usage of the stack-pr command.")))
        parser.print_help()
        return

    # Handle config command early since it doesn't need git repo setup
    if args.command == "config":
        command_config(config_file, args.setting)
        return

    # These commands don't need gh/target/stack setup either.
    if args.command == "help":
        command_help(parser, args.topic)
        return
    if args.command == "install":
        command_install(args.name, local=args.local)
        return

    # Make sure "$ID" is present in the branch name template and append it if not
    args.branch_name_template = fix_branch_name_template(args.branch_name_template)
    common_args = CommonArgs.from_args(
        args,
        land_disabled=(
            config.get("land", "style", fallback="bottom-only") == "disable"
        ),
    )

    if common_args.verbose:
        logger.setLevel(logging.DEBUG)

    check_gh_installed()

    current_branch = get_current_branch_name()
    get_branch_name_base(common_args.branch_name_template)
    stashed_changes = False
    try:
        if args.command in ["submit", "export"] and args.stash:
            result = run_shell_command(
                ["git", "stash", "save"], quiet=not common_args.verbose
            )
            # Check if stash actually saved anything
            # git stash outputs "No local changes to save" when there's nothing to stash
            output = result.stdout.decode() if result.stdout else ""
            stashed_changes = "No local changes to save" not in output

        # autoland may operate in a temporary worktree (--branch), so the
        # primary checkout being dirty shouldn't block it.
        if args.command not in ("view", "autoland") and not is_repo_clean():
            error(ERROR_REPO_DIRTY)
            return
        check_target_branch_exists(common_args)
        # autoland deduces its own base: with --branch it operates in a
        # temporary worktree, so the base must be resolved against that
        # worktree's HEAD rather than the primary checkout's HEAD (which may be
        # a different branch entirely).
        if args.command != "autoland":
            common_args = deduce_base(common_args)

        if args.command in ["submit", "export"]:
            command_submit(
                common_args,
                draft=args.draft,
                reviewer=args.reviewer,
                keep_body=args.keep_body,
                draft_bitmask=args.draft_bitmask,
            )
        elif args.command == "land":
            command_land(common_args)
        elif args.command == "abandon":
            command_abandon(common_args)
        elif args.command == "adopt":
            command_adopt(
                common_args,
                getattr(args, "pr", None),
                getattr(args, "commit", None),
            )
        elif args.command == "view":
            command_view(common_args)
        elif args.command == "autoland":
            from stack_pr import autoland  # noqa: PLC0415

            autoland.run_autoland(common_args, args, config)
        else:
            print(h(red("Unknown command: " + args.command)))
            return
    except Exception as exc:
        # If something failed, checkout the original branch
        run_shell_command(
            ["git", "checkout", current_branch], quiet=not common_args.verbose
        )
        if isinstance(exc, SubprocessError):
            print_cmd_failure_details(exc)
        raise
    finally:
        if args.command in ["submit", "export"] and args.stash and stashed_changes:
            run_shell_command(["git", "stash", "pop"], quiet=not common_args.verbose)


if __name__ == "__main__":
    main()
