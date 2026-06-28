# Top of tree

* Added an `adopt` command to bring an existing, normally-created PR under
  stack-pr management without closing and recreating it. Supports a `--commit`
  option to attach the PR to a specific commit (e.g. when inserting a new PR
  underneath an existing one) (#121).
* The `Stacked PRs:` cross-links list is now maintained as PRs land: merged and
  closed PRs are kept in the list of later PRs instead of disappearing after a
  subsequent `submit` (#53).

# Version 0.1.3

* Fix a bug with replacing $USERNAME in the branch name. (#44)

# Version 0.1.2

* Added config files - now defaults for the CL options can be customized with
  local config files (#32).
* Added a feature to customize branch names for stacked PRs (#33).
* Fixed a bug with branches not being deleted when a stack is abandoned (#27).
* Subcommands outputs is suppressed for less spammy look (#26).

# Version 0.1.1
