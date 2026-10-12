# Changelog

## 0.3.0

* Highlights `x <command>` steps, which run a command and stop the land
  unless it exits 0: the `x` keyword, and the command as a string. A bare `x`
  highlights as an error, since the parser rejects it. The formatter
  collapses the gap after `x` like any other step, and, as for `c`, an `x`
  step never sets the comment column.

## 0.2.0

* The extension is now a formatter for the language: **Format Document**
  aligns trailing comments at column 30 within each block of contiguous lines
  (or four spaces past the longest `l`/`w` step, when one reaches that far;
  `c` steps never set the column), collapses the gap after a step keyword,
  unindents steps, drops trailing whitespace and normalizes the final newline.
  Formatting never changes what a plan does.
* A tab before `#` no longer highlights as a comment: the parser only treats
  `' #'` (space then hash) as one, so `l 101<TAB># x` is a malformed step.

## 0.1.0

* Initial release: syntax highlighting for `stack-pr autoland` plan files
  (`l` / `w` / `c` steps, `#` comments, and `invalid` scopes for steps the
  `autoland` parser rejects).
