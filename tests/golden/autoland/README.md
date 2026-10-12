# Autoland golden files

Expected output of the end-to-end autoland scenarios in
`tests/test_autoland_golden.py`. Each scenario runs `stack-pr autoland` against
a scripted GitHub and local clone (`tests/autoland_scenarios.py`) and writes:

| File                         | What it holds                                                         |
| ---------------------------- | --------------------------------------------------------------------- |
| `<scenario>.out.txt`         | Everything the run printed (plain console, no rich), including input |
| `<scenario>.checkpoint.json` | The last checkpoint the run saved (a successful run deletes it after) |
| `<scenario>.status.txt`      | `stack-pr autoland --status` over that checkpoint                     |
| `<scenario>.status.json`     | `stack-pr autoland --status -o json` over that checkpoint             |
| `<scenario>.plan.txt`        | That checkpoint's plan, as the `-i` editor would show it              |

Temporary paths are replaced with `<TMP>` and the checkpoint's save time with
`<SAVED>`; everything else (SHAs included) is deterministic.

## Regenerating

When a change to autoland's behavior is intended, regenerate the files and
review the diff like any other change:

```bash
UPDATE_GOLDEN=1 uv run --no-sync pytest tests/test_autoland_golden.py
git diff tests/golden/autoland
```

A refactor that is meant to change nothing should pass without regenerating.
