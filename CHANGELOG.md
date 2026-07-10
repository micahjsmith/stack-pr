# Top of tree

* `autoland` can now land part of a stack instead of requiring the whole stack.
  Pass `-n/--count N` to land only the bottom `N` PRs (or, in `-i` mode, keep
  only the bottom PRs' `l` steps); the remaining PRs are rebased onto the landed
  commits and left open. Landing goes bottom-to-top, so a partial land is always
  a prefix of the stack. (#19)

* `submit`/`land`/`autoland` now push PR branches with `--force-with-lease`
  instead of a plain force-push. A branch changed on the remote out-of-band
  (e.g. a "Commit suggestion" accepted during review) is no longer silently
  overwritten — the push is rejected and stack-pr aborts with instructions to
  reconcile. See "Reconcile upstream changes" in the README. (#17)
* Fixed `autoland` re-submitting the wrong commit range after each merge. The
  stack base was deduced once at startup; after a PR merged and the stack was
  rebased onto an advanced target, that stale base made `submit` sweep in every
  commit merged by others in the meantime — trying to open bogus PRs for
  unrelated changes and aborting the land. `autoland` now re-deduces the base
  against the current target after each rebase. (#15)
* Fixed `submit` flipping existing ready PRs back to draft. To avoid closing
  PRs while branches are reordered, `submit` temporarily repointed their base
  branches and had marked them draft during that window; an interrupted run (or
  any error before the un-draft step) left them stuck as drafts. `submit` no
  longer touches the draft/ready status of existing PRs at all — that state is
  the user's to control. (#14)
* Fixed `autoland` workflow checkpoints (`w <workflow>`) polling forever in
  busy repos. The checkpoint targeted the current `origin/<target>` HEAD, which
  can advance past the landed PR's merge commit (bot commits, other PRs) between
  merge and the check — so a green workflow run on the actual merge commit was
  rejected as "too old". The checkpoint now targets the landed PR's exact merge
  commit. (#12)
* Fixed a crash during `submit` when a PR in the stack had been added to a
  GitHub merge queue. GitHub refuses to change such a PR's base branch, which
  previously aborted the whole submit; now stack-pr warns and leaves that PR's
  base unchanged while still updating its title/body.
* Added an `adopt` command to bring an existing, normally-created PR under
  stack-pr management without closing and recreating it. Supports a `--commit`
  option to attach the PR to a specific commit (e.g. when inserting a new PR
  underneath an existing one) (#121).
* The `Stacked PRs:` cross-links list is now maintained as PRs land: merged and
  closed PRs are kept in the list of later PRs instead of disappearing after a
  subsequent `submit` (#53).
* Added an `autoland` command that lands a whole stack through the GitHub merge
  queue (waits for approval/CI with flaky-check retries, enqueues bottom-to-top,
  rebases and re-submits after each merge, and supports `--resume`, `--branch`
  worktrees, and interactive checkpoints). Interactive (`-i`) landing plans
  support `w <workflow>` steps (wait for a named GitHub Actions workflow to
  complete with the landed code) and `c [condition]` confirmation steps between
  land steps (the optional condition names what to verify before proceeding and
  is shown in the prompt); setting `autoland.default_workflow` pre-fills the
  plan with a trailing `w <default_workflow>` step. A per-branch filesystem lock prevents
  two autolands from running on the same branch at once, and starting a fresh
  run over an existing checkpoint requires confirmation. Repo-specific settings live
  under `[autoland]` config; requires `autoland.merge_queue=true`. Install the
  optional `rich` extra for live progress tables. (#3, #7, #8, #9)
* Added an `install` command that registers stack-pr as a git alias (e.g.
  `git stack`), plus a `help` command so `git stack help` works (git intercepts
  `git stack --help` for aliases).

# Version 0.1.3

* Fix a bug with replacing $USERNAME in the branch name. (#44)

# Version 0.1.2

* Added config files - now defaults for the CL options can be customized with
  local config files (#32).
* Added a feature to customize branch names for stacked PRs (#33).
* Fixed a bug with branches not being deleted when a stack is abandoned (#27).
* Subcommands outputs is suppressed for less spammy look (#26).

# Version 0.1.1
