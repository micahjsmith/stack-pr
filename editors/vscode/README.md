# stack-pr Autoland Plan — VS Code syntax highlighting

Syntax highlighting for `stack-pr autoland` landing plans: the `l` / `w` / `c`
step files you edit with `autoland -i` or pass to `autoland --plan-file`.

See [`examples/example.autoland-plan`](examples/example.autoland-plan) for a
plan exercising every step type.

## What it highlights

| Plan syntax | Scope |
| --- | --- |
| `l`, `w`, `c` step keywords | `keyword.control.<step>.autoland-plan` |
| `l 123` — the PR number | `constant.numeric.pr-number.autoland-plan` |
| `l https://…/pull/123` — the PR URL | `markup.underline.link.autoland-plan` |
| `w deploy.yaml` — the workflow name | `entity.name.function.workflow.autoland-plan` |
| `c QA sign-off complete` — the condition | `string.unquoted.condition.autoland-plan` |
| `#` comments, whole-line and trailing | `comment.line.number-sign.autoland-plan` |
| a step the parser would reject | `invalid.illegal.*.autoland-plan` |

The `invalid` scopes mirror `parse_plan` in `stack_pr/autoland.py`, so a plan
that highlights as an error is a plan `autoland` will refuse to run: an `l` with
something that is neither a PR number nor a PR URL, a `w` with no workflow, or
a line that is not a step at all.

Two details worth knowing, both inherited from the parser:

- A `#` only starts a comment when whitespace precedes it. `l #123` is a bare
  `l` followed by a comment (which is why `l #123` does not pin PR 123), while
  `c ship it#now` keeps the `#` inside the condition.
- A workflow name runs to the end of the step, spaces included.

## File association

Applied automatically to:

- files named `*.autoland-plan`;
- `autoland-plan-*.txt` — the temporary file `autoland -i` opens in `$EDITOR`,
  so plans you edit interactively are highlighted without any setup;
- any file whose first line starts with `# Autoland plan`, which is the header
  of a generated plan.

To highlight a plan saved under another name, use **Change Language Mode** and
pick *Autoland Plan*, or associate the name in your settings:

```json
"files.associations": { "plan.txt": "autoland-plan" }
```

## Install

From this directory:

```bash
# Package and install into VS Code.
npx --yes @vscode/vsce package
code --install-extension autoland-plan-0.1.0.vsix
```

Or, for development, symlink it into your extensions directory and reload:

```bash
ln -s "$PWD" ~/.vscode/extensions/autoland-plan
```

## Develop

The grammar lives in [`syntaxes/autoland-plan.tmLanguage.json`](syntaxes/autoland-plan.tmLanguage.json).
Tests are assertion comments inside a plan file — the comment syntax is the
same `#`, so a test file is also a valid plan:

```bash
npx --yes vscode-tmgrammar-test "tests/*.autoland-plan"
```

`tests/test_vscode_extension.py` in the repository root additionally checks
that [`examples/example.autoland-plan`](examples/example.autoland-plan) is
accepted by the real `parse_plan`, so the shipped example cannot drift from the
format the CLI implements.

## License

MIT — see [LICENSE](LICENSE). (The rest of the repository is under the Apache
License v2.0 with LLVM Exceptions; this directory is licensed separately so the
extension can be redistributed on its own.)
