"""stack-pr autoland: land a whole stack through the GitHub merge queue.

This package holds the full autoland engine. cli.py only wires up the
subparser and dispatches into `run_autoland` to keep cli.py small.

Repo-specific behavior (which CI checks gate a merge, poll intervals,
retry counts, workflow timeouts, and whether the repo uses a merge queue at
all) is externalized to the `[autoland]` config section and command-line
flags, so a repo can reproduce its workflow with configuration alone.

Autoland automates stack-pr's own commands, so it uses cli's stack model:
CommonArgs, StackEntry, get_stack, deduce_base, command_submit, and last.
Plain shell, git, and gh helpers come from the shared modules instead.

Modules, roughly bottom of the dependency graph first:

- ``options``: AutolandOptions, resolved from flags, config, and defaults.
- ``runtime``: the console, sleep/wake resilience, and the retrying ``run``.
- ``model``: the stack, plan steps, and LandingContext, and their
  (de)serialization.
- ``state``: the checkpoint (``--resume``) and the per-branch lock.
- ``gh``: the GitHub client (every ``gh`` call autoland makes).
- ``worktree``: the temporary worktree for ``--branch``.
- ``discovery``: discovering the stack and enriching it from GitHub.
- ``checks``: evaluating CI checks.
- ``plan``: generating, formatting, parsing, and replanning landing plans.
- ``display``: rendering a plan's progress.
- ``waits``: waiting for approval, checks, the merge queue, and workflows.
- ``native_stack``: merging a run of land steps as one GitHub stack.
- ``engine``: executing a plan.
- ``status``: the ``--status`` report.
- ``commands``: the subparser, and the fresh / resume / replan flows.

Convention for shared state and patch targets: a module-level singleton
(``runtime.console``, the ``gh.github`` client, ``runtime.HAVE_RICH``,
``display.GLYPHS``) lives in exactly one module, and other modules reach it
through that module -- ``from stack_pr.autoland import runtime``, then
``runtime.console`` -- never ``from stack_pr.autoland.runtime import console``.
The same goes for a function that tests replace to intercept calls made from
another module: ``runtime.run``, ``runtime.resilient_sleep``, the
``waits.wait_for_*`` helpers, ``engine.execute_plan``, the ``discovery``
functions, and ``stack_pr.git.get_current_branch_name``. Each therefore has one
canonical patch target, e.g. ``mocker.patch("stack_pr.autoland.runtime.console")``,
and a patch can't silently miss a stale copy bound in another module. (A name
used by a single module, such as ``Worktree`` in ``commands``, is imported
directly and patched where it is used.) For the same reason, this package
re-exports only its entry points, not the singletons.
"""

from __future__ import annotations

from stack_pr.autoland.commands import register_parser, run_autoland

__all__ = ["register_parser", "run_autoland"]
