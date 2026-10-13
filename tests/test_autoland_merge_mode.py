"""Landing without a merge queue: resolving the merge mode, and merging directly."""

from __future__ import annotations

import argparse
import configparser
import dataclasses

import pytest

from stack_pr.autoland import native_stack
from stack_pr.autoland.merge_mode import MergeMode, MergeModeError, resolve_merge_mode
from stack_pr.autoland.model import LandingContext, PRState
from stack_pr.autoland.options import AutolandOptions
from tests.autoland_fakes import (
    OPTS,
    FakeGitHub,
    FakeServices,
    FakeSleep,
    FakeWaits,
    stack,
)
from tests.helpers import common_args

DIRECT = dataclasses.replace(OPTS, merge_mode=MergeMode.DIRECT)


# --- resolving the mode ------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"), [("true", True), ("false", False), (None, None)]
)
def test_merge_queue_config_is_an_optional_override(
    value: str | None, expected: bool | None
) -> None:
    cfg = configparser.ConfigParser()
    cfg["autoland"] = {} if value is None else {"merge_queue": value}

    opts = AutolandOptions.from_config_and_args(cfg, argparse.Namespace())

    assert opts.merge_queue is expected


@pytest.mark.parametrize(
    ("configured", "expected"), [(True, MergeMode.QUEUE), (False, MergeMode.DIRECT)]
)
def test_configured_mode_wins_without_asking_github(
    configured: bool, expected: MergeMode
) -> None:
    github = FakeGitHub()

    assert resolve_merge_mode("main", github, configured=configured) is expected  # type: ignore[arg-type]
    assert github.calls == []


@pytest.mark.parametrize(
    ("has_queue", "expected"), [(True, MergeMode.QUEUE), (False, MergeMode.DIRECT)]
)
def test_unset_mode_follows_whether_the_target_has_a_queue(
    has_queue: bool, expected: MergeMode
) -> None:
    github = FakeGitHub()
    github.script("has_merge_queue", has_queue)

    assert resolve_merge_mode("release", github, configured=None) is expected  # type: ignore[arg-type]
    assert github.called("has_merge_queue") == [("release",)]


def test_unset_mode_fails_when_github_cannot_say() -> None:
    github = FakeGitHub()
    github.script("has_merge_queue", None)

    with pytest.raises(MergeModeError, match=r"autoland\.merge_queue=false"):
        resolve_merge_mode("main", github, configured=None)  # type: ignore[arg-type]


# --- merging one PR directly ---------------------------------------------------


def _merge(fakes: FakeServices, opts: AutolandOptions = DIRECT):  # noqa: ANN202
    """Merge PR #101, whose checks pass, the way *opts* says."""
    fakes.github.pr_checks[101] = [{"name": "ci", "bucket": "pass"}]
    entry = stack(101)[0]
    return fakes.services.waits.merge_and_wait(entry, opts=opts), entry


def test_direct_merge_squashes_with_the_stack_pr_land_message() -> None:
    fakes = FakeServices.polling()

    merged, entry = _merge(fakes)

    assert merged
    assert entry.state == PRState.MERGED
    # The commit message without its stack-info trailer, numbered like
    # `stack-pr land` numbers it, pinned to the head it was read from.
    assert fakes.github.squash_merges == [
        (101, "Title 101 (#101)", "\nBody of 101.", "head-101")
    ]
    assert fakes.github.enqueued == []


def test_queue_mode_still_enqueues() -> None:
    fakes = FakeServices.polling()

    merged, _entry = _merge(fakes, OPTS)

    assert merged
    assert fakes.github.enqueued == [101]
    assert fakes.github.squash_merges == []


def test_direct_merge_retries_a_failed_merge_after_rechecking() -> None:
    fakes = FakeServices.polling()
    fakes.github.script("squash_merge", RuntimeError("Base branch was modified"))

    merged, entry = _merge(fakes, dataclasses.replace(DIRECT, max_queue_retries=1))

    assert merged
    assert entry.queue_retries == 1
    assert len(fakes.github.squash_merges) == 1
    # Approval and checks were verified again before the retry.
    assert len(fakes.github.called("review_decision")) == 1
    assert len(fakes.github.called("checks")) == 1
    assert "Base branch was modified" in fakes.ui.output


def test_direct_merge_gives_up_after_its_retries() -> None:
    fakes = FakeServices.polling()
    fakes.github.script(
        "squash_merge", RuntimeError("not mergeable"), RuntimeError("not mergeable")
    )

    merged, entry = _merge(fakes, dataclasses.replace(DIRECT, max_queue_retries=1))

    assert not merged
    assert entry.state == PRState.FAILED
    assert entry.error_message == "Failed to merge after 2 attempts: not mergeable"


def test_direct_merge_that_errored_but_went_through_is_not_retried() -> None:
    fakes = FakeServices.polling()

    def lost_response(pr: int, **_kwargs: object) -> None:
        fakes.github.merged.add(pr)
        raise RuntimeError("unexpected EOF")

    fakes.github.script("squash_merge", lost_response)

    merged, entry = _merge(fakes)

    assert merged
    assert entry.queue_retries == 0


def test_direct_merge_waits_for_github_to_report_it() -> None:
    fakes = FakeServices.polling()
    fakes.github.script("pr_state", "OPEN", "OPEN")  # lagging behind the merge

    merged, entry = _merge(fakes)

    assert merged
    assert entry.queue_retries == 0
    assert len(fakes.github.squash_merges) == 1


def test_direct_merge_of_a_closed_pr_fails() -> None:
    fakes = FakeServices.polling()
    fakes.github.closed.add(101)

    merged, entry = _merge(fakes)

    assert not merged
    assert entry.error_message == "PR was closed"
    assert fakes.github.squash_merges == []


def test_direct_merge_waits_out_a_branch_that_is_behind() -> None:
    sleep = FakeSleep()
    fakes = FakeServices.polling(sleep=sleep)
    behind = {"state": "OPEN", "mergeStateStatus": "BEHIND", "mergeable": "MERGEABLE"}
    fakes.github.script("merge_state", behind, behind)

    merged, _entry = _merge(fakes)

    assert merged
    assert len(sleep.slept) == 2
    # Says why it is waiting, and what to do, once.
    assert fakes.ui.output.count("stack-pr submit") == 1


# --- merging a run as a GitHub stack, directly --------------------------------


def test_direct_stack_merge_is_not_mistaken_for_a_boot() -> None:
    github = FakeGitHub()
    github.booted = {101, 102}  # nothing is ever in a queue
    github.lands = []  # the request is still pending at the first poll
    github.script("merge_async_status", ("pending", ""))

    def merge_while_asleep(_seconds: int) -> float:
        github.merged.update({101, 102})
        return 0.0

    fakes = FakeServices(github=github, sleep=merge_while_asleep, waits=FakeWaits())
    entries = stack(101, 102)

    result = native_stack.land_as_native_stack(
        entries,
        ctx=LandingContext(stack=entries),
        common=common_args(),
        opts=DIRECT,
        services=fakes.services,
    )

    assert result.landed
    assert github.merge_via_queue == [False]
