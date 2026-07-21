# Stacked PRs for GitHub

This is a command-line tool that helps you create multiple GitHub
pull requests (PRs) all at once, with a stacked order of dependencies.

Imagine that we have a change `A` and a change `B` depending on `A`, and we
would like to get them both reviewed. Without stacked PRs one would have to
create two PRs: `A` and `A+B`. The second PR would be difficult to review as it
includes all the changes simultaneously. With stacked PRs the first PR will
have only the change `A`, and the second PR will only have the change `B`. With
stacked PRs one can group related changes together making them easier to
review.

Example:

![StackedPRExample1](https://modular-assets.s3.amazonaws.com/images/stackpr/example_0.png)

## Comparison

There are several tool that help with stacked PRs.

- stack-pr (this project): A stack is a set of commits on a branch. Each commit is one PR. The commits are modified over time via a interactive rebase-based workflow. Each PR description automatically gets a preamble section showing the full stack. The tool manages a mapping from commit => temp branch => PR with state stored in the commit body itself. Support for GitHub only via the `gh` CLI.
- [git-spice](https://abhinav.github.io/git-spice/): A stack is a set of branches. Each branch is one PR. The branches are modified over time via normal git operations. Branches must be tracked by the tool with a separate local state store. A special restack commit rebases all the branches. Support for non-linear stack branches and multiple git hosting providers.
- [jj-stack](https://github.com/keanemind/jj-stack/): A stack is a set of Jujutsu bookmarks. Jujutsu already conceptually supports stacks locally, so this tool focuses on syncing the local repo state to GitHub.

## Installation

### Dependencies

This is a non-comprehensive list of dependencies required by `stack-pr`:

- Install `gh`, e.g., `brew install gh` on MacOS.
- Run `gh auth login` with SSH


### Installation with `pipx`

This fork is not published to PyPI. To install it directly from the GitHub repo
via [pipx](https://pipx.pypa.io/stable/) run:

```bash
pipx install 'stack-pr[rich] @ git+https://github.com/micahjsmith/stack-pr.git'
```

### Manual installation from source

Manually, you can clone the repository and run the following command:

```bash
pipx install .
```

## Usage

`stack-pr` allows you to work with stacked PRs: submit, view, and land them.

### Use as a git subcommand

If you'd rather drive the tool through `git`, install it as a git alias:

```bash
stack-pr install
```

```bash
git stack view
git stack submit
```
### Basic Workflow

The most common workflow is simple:

1. Create a feature branch from `main`:
```bash
git checkout main
git pull
git checkout -b my-feature
```

2. Make your changes and create multiple commits (one commit per PR you want to create)
```bash
# Make some changes
git commit -m "First change"
# Make more changes
git commit -m "Second change"
# And so on...
```

3. Review what will be in your stack:
```bash
stack-pr view  # Always safe to run, helps catch issues early
```

4. Create/update the stack of PRs:
```bash
stack-pr submit
```
> **Note**: `export` is an alias for `submit`.

5. To update any PR in the stack:
- Amend the corresponding commit
- Run `stack-pr view` to verify your changes
- Run `stack-pr submit` again

6. To rebase your stack on the latest main:
```bash
git checkout my-feature
git pull origin main  # Get the latest main
git rebase main       # Rebase your commits on top of main
stack-pr submit       # Resubmit to update all PRs
```

7. When your PRs are ready to merge, you have three options:

**Option A**: Using `stack-pr land`:
```bash
stack-pr land
```
This will:
- Merge the bottom-most PR in your stack
- Automatically rebase your remaining PRs
- You can run `stack-pr land` again to merge the next PR once CI passes

**Option B**: Using GitHub web interface:
1. Merge the bottom-most PR through GitHub UI
2. After the merge, on your local machine:
   ```bash
   git checkout my-feature
   git pull origin main  # Get the merged changes
   stack-pr submit       # Resubmit the stack to rebase remaining PRs
   ```
3. Repeat for each PR in the stack

**Option C**: Using `stack-pr autoland` (if your repo uses GitHub merge queue):
```bash
stack-pr autoland
```

That's it!

> **Pro-tip**: Run `stack-pr view` frequently - it's a safe command that helps you understand the current state of your stack and catch any potential issues early.

### Commands

`stack-pr` has five main commands:

- `submit` (or `export`) - create a new stack of PRs from the given set of
  commits. One can think of this as "push my local changes to the corresponding
  remote branches and update the corresponding PRs (or create new PRs if they
  don't exist yet)".
- `view` - inspect the given set of commits and find the linked PRs. This
  command does not push any changes anywhere and does not change any commits.
  It can be used to examine what other commands did or will do.
- `abandon` - remove all stack metadata from the given set of commits. Apart
  from removing the metadata from the affected commits, this command deletes
  the corresponding local and remote branches and closes the PRs.
- `adopt` - bring an existing, normally-created PR under `stack-pr` management.
  This embeds stack metadata into the bottom-most commit pointing at that PR, so
  subsequent `submit` runs update the existing PR (preserving its review
  history) instead of creating a new one.
- `land` - merge the bottom-most PR in the current stack and rebase the rest of
  the stack on the latest main.

`stack-pr` also has several other commands:

- `autoland` - land the whole stack automatically through the GitHub merge
  queue: wait for approvals and CI (retrying flaky checks), enqueue each PR
  bottom-to-top, rebase and re-submit the rest after each merge, and resume
  cleanly after an interruption. Requires a repo that uses the GitHub merge
  queue (see `autoland.merge_queue` below).
- `config` - set configuration values in the config file. Similar to `git config`,
  it takes a setting in the format `<section>.<key>=<value>` and updates the
  config file (`.stack-pr.cfg` by default).
- `install` - install stack-pr as a git alias so it can be invoked as
  `git stack` (see [Use as a git subcommand](#use-as-a-git-subcommand)).
- `help` - print help. Useful as `git stack help`, since `git stack --help` is
  intercepted by git for aliases.

A usual workflow is the following:

```bash
while not ready to merge:
    make local changes
    commit to local git repo or amend existing commits
    create or update the stack with `stack-pr submit`
merge changes with `stack-pr land`
```

You can also use `view` at any point to examine the current state, and
`abandon` to drop the stack.

### How it works

Under the hood, the tool creates and maintains branches named
`$USERNAME/stack/$BRANCH_NUM` (the name pattern can be customized via
`--branch-name-template` option) and embeds stack metadata into commit messages (see [](#implementation-details)).

You don't work with those managed branches or edit that metadata
manually. Instead of pushing to these branches you should use `submit`,
instead of deleting them you should use `abandon` and instead of merging them
you should use `land`.

The tool looks at commits in the range `BASE..HEAD` and creates a stack of PRs
to apply these commits to `TARGET`. By default, `BASE` is `main` (local
branch), `HEAD` is the git revision `HEAD`, and `TARGET` is `main` on remote
(i.e. `origin/main`). These parameters can be changed with options `-B`, `-H`,
and `-T` respectively and accept the standard git notation: e.g. one can use
`-B HEAD~2`, to create a stack from the last two commits.

### Example

The first step before creating a stack of PRs is to double-check the changes
we’re going to post.

By default `stack-pr` will look at commits in `main..HEAD` range and will create
a PR for every commit in that range.

For instance, if we have

```bash
# git checkout my-feature
# git log -n 4  --format=oneline
**cc932b71c** (**my-feature**)        Optimized navigation algorithms for deep space travel
**3475c898f**                         Fixed zero-gravity coffee spill bug in beverage dispenser
**99c4cd9a7**                         Added warp drive functionality to spaceship engine.
**d2b7bcf87** (**origin/main, main**) Added module for deploying remote space probes

```

Then the tool will consider the top three commits as changes, for which we’re
trying to create a stack.

> **Pro-tip**: a convenient way to see what commits will be considered by
> default is the following command:
>

```bash
alias githist='git log --abbrev-commit --oneline $(git merge-base origin/main HEAD)^..HEAD'
```

We can double-check that by running the script with `view` command - it is
always a safe command to run:

```bash
# stack-pr view
...
VIEW
**Stack:**
   * **cc932b71** (No PR): Optimized navigation algorithms for deep space travel
   * **3475c898** (No PR): Fixed zero-gravity coffee spill bug in beverage dispenser
   * **99c4cd9a** (No PR): Added warp drive functionality to spaceship engine.
SUCCESS!
```

If everything looks correct, we can now submit the stack, i.e. create all the
corresponding PRs and cross-link them. To do that, we run the tool with
`submit` command:

```bash
# stack-pr submit
...
SUCCESS!
```

The command accepts a couple of options that might be useful, namely:

- `--draft` - mark all created PRs as draft. This helps to avoid over-burdening
  CI.
- `--draft-bitmask` - mark select PRs in a stack as draft using a bitmask where
    `1` indicates draft, and `0` indicates non-draft.
    For example `--draft-bitmask 0010` to make the third PR a draft in a stack
    of four.
    The length of the bitmask must match the number of stacked PRs.
    Overridden by `--draft` when passed.
- `--reviewer="handle1,handle2"` - assign specified reviewers.

If the command succeeded, we should see “SUCCESS!” in the end, and we can now
run `view` again to look at the new stack:

```python
# stack-pr view
...
VIEW
**Stack:**
   * **cc932b71** (#439, 'ZolotukhinM/stack/103' -> 'ZolotukhinM/stack/102'): Optimized navigation algorithms for deep space travel
   * **3475c898** (#438, 'ZolotukhinM/stack/102' -> 'ZolotukhinM/stack/101'): Fixed zero-gravity coffee spill bug in beverage dispenser
   * **99c4cd9a** (#437, 'ZolotukhinM/stack/101' -> 'main'): Added warp drive functionality to spaceship engine.
SUCCESS!
```

We can also go to github and check our PRs there:

![StackedPRExample2](https://modular-assets.s3.amazonaws.com/images/stackpr/example_1.png)

If we need to make changes to any of the PRs (e.g. to address the review
feedback), we simply amend the desired changes to the appropriate git commits
and run `submit` again. If needed, we can rearrange commits or add new ones.

`submit` simply syncs the local changes with the corresponding PRs. This is why
we use the same `stack-pr submit` command when we create a new stack, rebase our
changes on the latest main, update any PR in the stack, add new commits to the
stack, or rearrange commits in the stack.

When we are ready to merge our changes, we use `land` command.

```python
# stack-pr land
LAND
Stack:
   * cc932b71 (#439, 'ZolotukhinM/stack/103' -> 'ZolotukhinM/stack/102'): Optimized navigation algorithms for deep space travel
   * 3475c898 (#438, 'ZolotukhinM/stack/102' -> 'ZolotukhinM/stack/101'): Fixed zero-gravity coffee spill bug in beverage dispenser
   * 99c4cd9a (#437, 'ZolotukhinM/stack/101' -> 'main'): Added warp drive functionality to spaceship engine.
Landing 99c4cd9a (#437, 'ZolotukhinM/stack/101' -> 'main'): Added warp drive functionality to spaceship engine.
...
Rebasing 3475c898 (#438, 'ZolotukhinM/stack/102' -> 'ZolotukhinM/stack/101'): Fixed zero-gravity coffee spill bug in beverage dispenser
...
Rebasing cc932b71 (#439, 'ZolotukhinM/stack/103' -> 'ZolotukhinM/stack/102'): Optimized navigation algorithms for deep space travel
...
SUCCESS!
```

This command lands the first PR of the stack and rebases the rest. If we run
`view` command after `land` we will find the remaining, not yet-landed PRs
there:

```python
# stack-pr view
VIEW
**Stack:**
   * **8177f347** (#439, 'ZolotukhinM/stack/103' -> 'ZolotukhinM/stack/102'): Optimized navigation algorithms for deep space travel
   * **35c429c8** (#438, 'ZolotukhinM/stack/102' -> 'main'): Fixed zero-gravity coffee spill bug in beverage dispenser
```

This way we can land all the PRs from the stack one by one.

### Specifying custom commit ranges

The example above used the default commit range - `main..HEAD`, but you can
specify a custom range too. Below are several commonly useful invocations of
the script:

```bash
# Submit a stack of last 5 commits
stack-pr submit -B HEAD~5

# Use 'origin/main' instead of 'main' as the base for the stack
stack-pr submit -B origin/main

# Do not include last two commits to the stack
stack-pr submit -H HEAD~2
```

These options work for all script commands (and it’s recommended to first use
them with `view` to double check the result). It is possible to mix and match
them too - e.g. one can first submit the stack for the last 5 commits and then
land first three of them:

```bash
# Inspect what commits will be included HEAD~5..HEAD
stack-pr view -B HEAD~5
# Create a stack from last five commits
stack-pr submit -B HEAD~5

# Inspect what commits will be included into the range HEAD~5..HEAD~2
stack-pr view -B HEAD~5 -H HEAD~2
# Land first three PRs from the stack
stack-pr land -B HEAD~5 -H HEAD~2
```

Note that generally one doesn't need to specify the base and head branches
explicitly - `stack-pr` will figure out the correct range based on the current
branch and the remote `main` by default.

### Reconcile upstream changes

`stack-pr` treats your local commits as the source of truth and force-pushes
each PR's branch to match. If a PR branch is changed directly on the remote —
most often by accepting a **"Commit suggestion"** during review, or editing a
file through the GitHub web UI — that commit exists only on the remote, not in
your local stack.

`stack-pr` pushes with `--force-with-lease`, so instead of silently discarding
such a change, the next `submit` (or `land`/`autoland`) stops with an error
naming the affected branch. Reconcile it by folding the upstream commit into the
local commit that backs that PR:

1. Fetch the upstream state. This also updates the remote-tracking ref so the
   next push is allowed to proceed:
   ```bash
   git fetch origin
   ```
2. Start an interactive rebase over your stack and mark the affected commit
   `edit` (git stops on it with the commit already applied):
   ```bash
   git rebase -i origin/main
   ```
3. Apply the upstream commit(s) and fold them into that commit:
   ```bash
   git cherry-pick -n origin/<pr-branch>   # the branch named in the error;
                                           # use a range A^..B for multiple
   git commit --amend --no-edit
   git rebase --continue
   ```
4. Re-submit the stack:
   ```bash
   stack-pr submit
   ```

The change now lives in the single commit that backs the PR, so the stack stays
one-commit-per-PR and future updates won't lose it.

## Command Line Options Reference

### Common Arguments

These arguments can be used with any subcommand:

- `-R, --remote`: Remote name (default: "origin")
- `-B, --base`: Local base branch
- `-H, --head`: Local head branch (default: "HEAD")
- `-T, --target`: Remote target branch (default: "main")
- `--hyperlinks/--no-hyperlinks`: Enable/disable hyperlink support (default: enabled)
- `-V, --verbose`: Enable verbose output from Git subcommands (default: false)
- `--branch-name-template`: Template for generated branch names (default: "$USERNAME/stack"). The following variables are supported:
   - `$USERNAME`: The username of the current user
   - `$BRANCH`: The current branch name
   - `$ID`: The location for the ID of the branch. The ID is determined by the order of creation of the branches. If `$ID` is not found in the template, the template will be appended with `/$ID`.

### Subcommands

#### submit (alias: export)

Submit a stack of PRs.

Options:

- `--keep-body` / `--no-keep-body`: Keep the current PR body instead of regenerating it from the commit message on every submit (default: true). Pass `--no-keep-body` to overwrite the body from the commit.
- `--keep-title` / `--no-keep-title`: Keep the current PR title instead of overwriting it from the commit subject on every submit (default: true). Pass `--no-keep-title` to overwrite the title from the commit.
- `-d, --draft`: Submit PRs in draft mode (default: false)
- `--draft-bitmask`: Bitmask for setting draft status per PR
- `--reviewer`: List of reviewers for the PRs (default: from $STACK_PR_DEFAULT_REVIEWER or config)
- `-s, --stash`: Stash all uncommitted changes before submitting the PR

#### land

Land the bottom-most PR in the current stack.

If the `land.style` config option has the `disable` value, this command is not available.

#### abandon

Abandon the current stack.

Takes no additional arguments beyond common ones.

#### adopt

Bring an existing, normally-created PR under `stack-pr` management. This is
useful when you opened a PR the usual way (with its own review history) and now
want to stack more PRs on top of it.

By default `adopt` looks at the bottom-most commit of the current range
(`main..HEAD` by default) and embeds stack metadata into it pointing at the
target PR. Once adopted, run `stack-pr submit` to update that PR and push the
rest of the stack; the original PR is updated in place rather than closed and
recreated.

Arguments:

- `pr` (optional): PR number or URL to adopt. If omitted, the PR associated with
  the currently checked-out branch is used.

Options:

- `--commit`: Commit (any git revision) to attach the PR to, when it isn't the
  bottom-most one - for example when inserting a new PR underneath an existing
  one. If omitted, the bottom-most commit of the stack is used.

Notes:

- The PR must be in the `OPEN` state.
- The PR's existing head branch is preserved (recorded in the metadata), so its
  review history and URL are kept.
- `submit` will force-push the adopted commit to the PR's branch, so if you
  have squashed or rebased since opening the PR, its diff will be updated
  accordingly. `adopt` warns when the local commit's contents differ from the
  PR's head.

Typical workflow (stack a new PR *on top* of an existing one):

```bash
# You already have an open PR for branch 'my-feature'.
git checkout my-feature
stack-pr adopt                  # adopt the existing PR for 'my-feature' first
git commit -m "Second change"   # stack a new change on top
stack-pr view                   # confirm the bottom PR is now managed
stack-pr submit                 # update the existing PR + create the new one
```

Stacking a new PR *underneath* an existing one (so the new change lands first):

1. Collapse the branch into a single commit (one commit == one PR) with an
   interactive rebase, marking every commit except the first as `squash` (or
   `fixup`):

   ```bash
   git checkout my-feature
   git rebase -i $(git merge-base origin/main HEAD)
   ```

2. Adopt the existing PR onto that commit while it is still the bottom commit:

   ```bash
   stack-pr adopt
   ```

3. Insert the new change beneath the adopted commit by building it on top of
   `main` and rebasing the adopted commit onto it:

   ```bash
   git checkout -b tmp-new-bottom origin/main
   # ... make the new change ...
   git commit -m "New change (lands first)"
   git rebase --onto tmp-new-bottom origin/main my-feature
   git branch -D tmp-new-bottom
   ```

4. Review and submit the stack:

   ```bash
   stack-pr view     # new commit at the bottom, existing PR on top
   stack-pr submit   # creates the new PR; re-bases the existing one onto it
   ```

Alternatively, build the stack in any order first and then adopt the existing
PR onto the right commit directly with `stack-pr adopt --commit <ref>`.

#### autoland

Land the entire stack automatically through the GitHub merge queue, one PR at a
time from the bottom up. Run it from the repo root while on the stack's head
branch. For each PR it waits for approval and CI (re-running flaky checks),
adds the PR to the merge queue (retrying if the PR is booted), and after each
merge rebases and re-submits the rest of the stack. Progress is checkpointed so
an interrupted run can be resumed.

> **Note**: `autoland` currently supports only repositories that use the GitHub
> merge queue. On other repositories it fails fast with a "not implemented"
> error; use `stack-pr land` instead. (Direct-merge autoland is future work.)

Options:

- `--dry-run`: Discover and display the stack, then exit.
- `-n, --count N`: Land only the bottom `N` PRs of the stack, leaving the rest
  open (they are rebased onto the newly-landed commits). Defaults to the whole
  stack. Landing goes bottom-to-top, so this always lands a prefix of the stack
  — useful for landing one (or a few) ready PRs at a time. In `-i` mode you can
  do the same by keeping only the bottom PRs' `l` steps.
- `--branch BRANCH`: Land a stack rooted on `BRANCH` using a temporary worktree
  (so your current checkout is left untouched). The worktree is removed on
  success and preserved on failure for debugging.
- `--always-cleanup`: Always remove the temporary worktree, even on failure.
- `-i, --interactive`: Edit the landing plan in `$EDITOR` first, inserting
  `workflow` checkpoints (`w <workflow>` — wait for a named GitHub Actions
  workflow to complete with the landed code) and `confirm` checkpoints (`c
  [condition]` — pause for manual confirmation) between land steps. The
  `condition` after `c` is optional: it names what you want to verify before
  proceeding (e.g. `c QA sign-off complete`), and is shown in the prompt when
  autoland reaches that step (`Confirm "QA sign-off complete" is complete —
  ready to proceed?`). A bare `c` just prompts `Ready to proceed?`. When
  `autoland.default_workflow` is configured, the pre-filled plan already ends
  with a `w <default_workflow>` step, which you can edit or delete.
- `--resume`: Resume a previously interrupted run from its checkpoint.
- `--state-file PATH`: Override the checkpoint path (default:
  `~/.stack-pr/autoland/<branch>.json`).
- `--poll-interval`, `--max-check-retries`, `--max-queue-retries`,
  `--workflow-timeout`: Override the corresponding `[autoland]` config values.

Only one `autoland` can run against a given branch at a time: while a run is in
progress it holds a per-branch lock (`~/.stack-pr/autoland/<branch>.json.lock`)
next to its checkpoint, so a second `autoland` on the same branch exits
immediately rather than racing the first. The lock is released automatically
when the run ends — including on failure or Ctrl+C — while the checkpoint is
kept so you can `--resume`. If you start a *new* (non-`--resume`) `autoland`
while a checkpoint from a previous run still exists, `autoland` warns that a
land is already in progress and asks you to confirm before overwriting it (the
previous run then can no longer be resumed).

Everything repo-specific is configured under `[autoland]` (see [Config
files](#config-files)), so a repository captures its workflow in
`.stack-pr.cfg`:

```ini
[repo]
target = main
[autoland]
merge_queue = true
required_checks = test,lint
poll_interval = 120
max_check_retries = 3
max_queue_retries = 3
default_workflow = deploy.yaml
```

- `merge_queue` (default `false`): must be `true` to enable `autoland`.
- `required_checks` (default empty): comma-separated CI check names that gate a
  merge. When empty, all reported (non-skipped) checks must pass.
- `default_workflow` (default empty): when set, an interactive (`-i`) landing
  plan is pre-filled with a `w <default_workflow>` step after the land steps,
  so a repo's usual post-land workflow wait is there by default (still
  editable/removable in `$EDITOR`).

Richer live progress tables are shown when the optional `rich` dependency is
installed (`pipx install 'stack-pr[rich]'` or add the `rich` extra); otherwise
`autoland` prints plain-text status.

##### Example plan

With `-i`, `autoland` opens a plan in `$EDITOR`. Each non-comment line is one
step, run top to bottom: `l` lands the next PR in the stack, `w <workflow>`
waits for a named GitHub Actions workflow to finish with the landed code, and
`c [condition]` pauses for manual confirmation (the condition is optional). For
a three-PR stack, a plan that lands the bottom PR, waits for a deploy, gets
manual sign-off, then lands the rest looks like:

```
# Autoland plan — edit steps below.
# l             = land the next PR in the stack
# w <workflow>  = wait for a workflow to complete
# c [condition] = pause for manual confirmation; the optional
#                 condition names what to verify before proceeding
#                 (e.g. 'c QA sign-off complete')
#
# Lines starting with # are comments and are ignored.
# Blank lines are ignored.
#
l                        # PR #101: Add /widgets API endpoint
w deploy.yaml            # wait for the deploy of PR #101 to finish
c QA sign-off complete   # pause until QA has signed off
l                        # PR #102: Wire up the widgets UI
l                        # PR #103: Update the docs
```

When `autoland` reaches the `c` step it prompts
`Confirm "QA sign-off complete" is complete — ready to proceed?` and waits for
`y`/`Y`. A bare `c` with no condition just prompts `Ready to proceed?`.

#### view

Inspect the current stack

Takes no additional arguments beyond common ones.

#### config

Set a configuration value in the config file.

Arguments:

- `setting` (required): Configuration setting in format `<section>.<key>=<value>`

Examples:

```bash
# Set verbose mode
stack-pr config common.verbose=True

# Disable usage tips (hide verbose output after commands)
stack-pr config common.show_tips=False

# Set target branch
stack-pr config repo.target=master

# Set default reviewer(s)
stack-pr config repo.reviewer=user1,user2

# Set custom branch name template
stack-pr config repo.branch_name_template=$USERNAME/stack

# Disable the land command (require GitHub web interface for merging)
stack-pr config land.style=disable

# Use "bottom-only" landing style for stacks
stack-pr config land.style=bottom-only
```

The config command modifies the config file (the `.stack-pr.cfg` file in the repo root by default, or the path specified by `STACKPR_CONFIG` environment variable). If the file doesn't exist, it will be created. If a setting already exists, it will be updated.

#### install

Install stack-pr as a git alias so it can be invoked as `git <name>` (see
[Use as a git subcommand](#use-as-a-git-subcommand)). This writes
`alias.<name> = !stack-pr` to your git config.

Options:

- `--name`: Alias name to create (default: `stack`, i.e. `git stack ...`).
- `--local`: Write to the current repository's git config instead of the
  global one.

#### help

Print help, optionally for a specific subcommand. This exists primarily so
`git stack help` works: git intercepts `git stack --help` for aliases and only
prints `'stack' is aliased to '!stack-pr'`.

Arguments:

- `topic` (optional): Subcommand to show help for, e.g. `stack-pr help submit`
  (or `git stack help submit`).

### Config files

Default values for command line options can be specified via a config file.
Path to the config file can be specified via `STACKPR_CONFIG` envvar, and by
default it's assumed to be `.stack-pr.cfg` in the current folder.

An example of a config file:

```cfg
[common]
verbose=True
hyperlinks=True
draft=False
keep_body=False
stash=False
show_tips=True

[repo]
remote=origin
target=main
reviewer=GithubHandle1,GithubHandle2
branch_name_template=$USERNAME/$BRANCH

[land]
style=bottom-only

[autoland]
merge_queue=True
required_checks=test,lint
poll_interval=120
max_check_retries=3
max_queue_retries=3
merge_timeout=3600
workflow_timeout=10800
default_workflow=deploy.yaml
```

## Implementation Details

### Stack metadata

`stack-pr` does not maintain any state outside of git itself. Instead, it
tracks the stack by embedding a metadata trailer into each managed commit
message:

```
stack-info: PR: https://github.com/<owner>/<repo>/pull/<number>, branch: <head-branch>
```

This single line is the source of truth for whether a commit is "managed":

- `submit` decides whether to **create** or **update** a PR for each commit by
  looking for this trailer. A commit without it gets a new branch and a new PR
  (and the trailer is then written back into the commit message); a commit with
  it has its existing PR updated.
- `view` reports the linked PR and branch for each commit by parsing the
  trailer (showing `No PR` when it is absent).
- `land` and `abandon` operate only on commits that carry a valid trailer.

The trailer records two fields: the **PR link** and the **head branch** that
`stack-pr` pushes for that commit (named via `--branch-name-template`, by
default `$USERNAME/stack/$ID`). The PR's base branch is not stored — it is
derived from the stack order at runtime (the previous commit's head branch, or
the target branch for the bottom-most PR).

Because the trailer is just text in the commit message, a commit can be brought
under `stack-pr` management by adding it (which is what `adopt` does for an
existing PR), and removed by deleting it (which is what `abandon` does).

### Cross-links between PRs

Each PR's body contains a `Stacked PRs:` table of contents listing every PR in
the stack (with `__->__` marking the current one). This list is regenerated on
each `submit`, and it is *maintained* as PRs land: a PR that has left the active
stack but has merged or been closed is kept in the list (pinned below the active
PRs in its original position), so later PRs retain the full history of the
stack. A PR that left the stack but is still open is dropped.

The list is stored only in the PR bodies - on each `submit`, `stack-pr` reads
the previously recorded list back from a surviving PR's body, re-checks the
status of any entries that are no longer in the active stack, and writes the
reconciled list to every PR. As a result, merged/closed entries are pinned at
the bottom of the list; inserting a brand-new commit *below* an
already-merged one is the one case where an entry can appear slightly out of
order.
