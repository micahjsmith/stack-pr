# stack-pr Autoland Plan — VS Code support

Syntax highlighting and formatting for `stack-pr autoland` landing plans: the
`l` / `w` / `c` step files you edit with `autoland -i` or pass to
`autoland --plan-file`.

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

## Formatting

The extension is a formatter for the language, so **Format Document**
(and `editor.formatOnSave`) tidies a plan. Formatting never changes what a plan
does — the formatted text parses into exactly the same steps:

- **Trailing comments are aligned at column 30** within each block of
  contiguous non-blank lines, so plans line up with each other and not just
  within a block. A blank line starts a new group. `autoland -i` writes this
  same layout, so a generated plan is already formatted — see
  [`examples/generated.autoland-plan`](examples/generated.autoland-plan).
- **A long `l` or `w` step widens its block**: if one reaches into column 30,
  that block's comments move to four spaces past the longest such step, so the
  block still lines up internally without affecting the rest of the file.
- **`c` steps never set the column.** A confirm condition is free text that
  routinely runs long, and letting it decide would drag every comment in the
  block off to the right. Their comments are still aligned with everything
  else; a condition already past the column keeps a single space before its
  `#`, since losing that space would fold the comment into the condition.
- **The gap after a step keyword is collapsed** to a single space, and steps are
  unindented. The argument itself is left alone: a condition is free text, and a
  workflow name may contain spaces.
- **Whole-line comments keep their own indentation**, since the generated plan
  header is hand-aligned. They do not break up a block.
- **Trailing whitespace goes**, and the file ends with exactly one newline.
- **Blank lines stay where you put them** — they are how a plan is grouped —
  and a line the parser would reject is passed through as you typed it.

A `#` that the parser would not treat as a comment is not treated as one here
either, so `c ship it#now` and `l 101<TAB># x` are left alone.

## File association

Applied automatically to:

- files named `*.autoland-plan` — the conventional suffix for a plan, and what
  `autoland -i` names the temporary file it opens in `$EDITOR`, so plans you
  edit interactively are highlighted without any setup;
- `autoland-plan-*.txt` — the name `-i` used before stack-pr adopted the
  `.autoland-plan` suffix, kept so plans from an older CLI still highlight;
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
code --install-extension autoland-plan-0.2.0.vsix
```

Or, for development, symlink it into your extensions directory and reload:

```bash
ln -s "$PWD" ~/.vscode/extensions/autoland-plan
```

## Develop

The grammar lives in [`syntaxes/autoland-plan.tmLanguage.json`](syntaxes/autoland-plan.tmLanguage.json)
and the formatter in [`src/format.js`](src/format.js), which is a plain
`string -> string` function so it can be tested without VS Code;
[`src/extension.js`](src/extension.js) only registers it. There is no build
step and no runtime dependency.

```bash
npm test            # both suites, no install needed
npm run test:grammar
npm run test:format
```

Grammar tests are assertion comments inside a plan file — the comment syntax is
the same `#`, so a test file is also a valid plan.

`tests/test_vscode_extension.py` in the repository root additionally checks
that [`examples/example.autoland-plan`](examples/example.autoland-plan) is
accepted by the real `parse_plan`, so the shipped example cannot drift from the
format the CLI implements.

## License

MIT — see [LICENSE](LICENSE). (The rest of the repository is under the Apache
License v2.0 with LLVM Exceptions; this directory is licensed separately so the
extension can be redistributed on its own.)
