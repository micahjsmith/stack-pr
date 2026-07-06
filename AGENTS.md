# Agent instructions

Guidance for AI coding agents (and humans) working in this repository.

## Manage stacked PRs with `stack-pr`

This repo *is* the `stack-pr` tool, and it uses `stack-pr` to manage its own
stacked pull requests. Whenever a change spans more than one PR, use the
installed `stack-pr` CLI rather than creating or juggling stacked branches by
hand.

Install it as described in the [README](./README.md):

```bash
pipx install 'stack-pr[rich] @ git+https://github.com/micahjsmith/stack-pr.git'
```

Common operations:

- `stack-pr view` — inspect the current stack (always safe).
- `stack-pr submit` (alias `export`) — create/update the stack of PRs.
- `stack-pr land` — merge the bottom PR and rebase the rest.
- `stack-pr autoland` — land the whole stack through a merge queue, where
  available.
- `stack-pr abandon` — drop the stack.

Repository defaults live in [`.stack-pr.cfg`](./.stack-pr.cfg) (target branch
`main`, remote `origin`).

## Pull requests target this fork

This repository is a fork of `modular/stack-pr`. Open pull requests against
**this fork (`micahjsmith/stack-pr`)**, not the upstream repository, unless the
user explicitly asks otherwise.

- Push branches to `origin` (this fork).
- With the `gh` CLI, pass `--repo micahjsmith/stack-pr` when creating or editing
  PRs, or run `gh repo set-default micahjsmith/stack-pr` once so it becomes the
  default. (For a fork, `gh` otherwise defaults to the upstream repo.)
- Never open, retarget, or push a PR to `modular/stack-pr` without explicit
  instruction.
- Never push directly to `main`; branch first.
