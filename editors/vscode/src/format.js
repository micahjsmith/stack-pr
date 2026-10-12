"use strict";

// Formatter for autoland plan files. Pure string -> string so it can be tested
// without VS Code; src/extension.js is the thin wrapper that registers it.
//
// The rules mirror parse_plan in stack_pr/autoland/plan.py, and none of them
// change what a plan does: a formatted plan parses into exactly the same steps.

// Trailing comments start here, so plans line up with each other and not just
// within one block. Kept in sync with format_plan_for_editor in
// autoland/plan.py.
const COMMENT_COLUMN = 30;
// ...unless a step reaches into that column, in which case the block's
// comments move this far past the longest one.
const COMMENT_GAP = 4;

// 'l', 'w' or 'c', optionally followed by an argument. The argument keeps its
// own spacing: a condition is free text and a workflow name may contain spaces.
const STEP = /^([lwc])[ \t]+(.*)$/;
// A step with no argument at all: a bare 'l' or 'c'.
const BARE_STEP = /^[lwc]$/;

/** Split a step line into its code and its trailing comment, if any. */
function splitComment(line) {
  // A '#' only opens a comment when a space precedes it, exactly as the parser
  // decides: 'c ship it#now' is all condition, 'l #123' is a bare 'l'.
  const at = line.indexOf(" #");
  if (at === -1) {
    return { code: line, comment: "" };
  }
  return { code: line.slice(0, at).trimEnd(), comment: line.slice(at + 1) };
}

/** Normalize one line into the pieces the alignment pass works on. */
function parseLine(raw) {
  const trimmed = raw.trim();
  if (trimmed === "") {
    return { kind: "blank" };
  }
  if (trimmed.startsWith("#")) {
    // A whole-line comment keeps its own indentation: the generated plan's
    // header is hand-aligned, and re-flowing it would only destroy that.
    return { kind: "comment", text: raw.trimEnd() };
  }
  const { code, comment } = splitComment(trimmed);
  const step = STEP.exec(code);
  // Collapse the run of whitespace after the step keyword to one space. An
  // unrecognized line is left alone: the parser will reject it, and guessing
  // at what it meant would be worse than leaving it as the author typed it.
  return {
    kind: "step",
    keyword: step ? step[1] : BARE_STEP.test(code) ? code : "",
    text: step ? `${step[1]} ${step[2]}` : code,
    comment,
  };
}

/**
 * Comment column for one block.
 *
 * Only 'l' and 'w' steps are measured. A 'c' condition is free text that
 * routinely runs past any sensible column, and letting it set one would drag
 * every comment in the block off to the right.
 */
function commentColumn(block) {
  const widths = block
    .filter((line) => line.keyword === "l" || line.keyword === "w")
    .map((line) => line.text.length);
  const longest = widths.length === 0 ? 0 : Math.max(...widths);
  return Math.max(COMMENT_COLUMN, longest + COMMENT_GAP);
}

function renderBlock(block) {
  const column = commentColumn(block);
  return block.map((line) => {
    if (line.kind === "comment" || !line.comment) {
      return line.text;
    }
    // A step already past the column (a long condition) cannot be aligned,
    // but it must still keep a space: without one the '#' would become part
    // of the step rather than a comment.
    const gap = Math.max(column - line.text.length, 1);
    return line.text + " ".repeat(gap) + line.comment;
  });
}

/**
 * Format an autoland plan.
 *
 * Trailing comments are aligned within each block of contiguous non-blank
 * lines, so a blank line starts a new alignment group. Blank lines are kept
 * as the author placed them — they are how a plan is grouped — except that
 * trailing ones are dropped in favour of a single final newline.
 */
function formatPlan(text) {
  const eol = text.includes("\r\n") ? "\r\n" : "\n";
  const lines = text.split(/\r?\n/).map(parseLine);

  const out = [];
  let block = [];
  const flush = () => {
    if (block.length > 0) {
      out.push(...renderBlock(block));
      block = [];
    }
  };
  for (const line of lines) {
    if (line.kind === "blank") {
      flush();
      out.push("");
    } else {
      block.push(line);
    }
  }
  flush();

  while (out.length > 0 && out[out.length - 1] === "") {
    out.pop();
  }
  return out.length === 0 ? "" : out.join(eol) + eol;
}

module.exports = { formatPlan, COMMENT_COLUMN, COMMENT_GAP };
