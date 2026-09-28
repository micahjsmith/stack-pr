"use strict";

const test = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");
const { formatPlan } = require("../src/format");

test("aligns trailing comments at a fixed column", () => {
  const src = ["l 101 # land it", "w deploy.yaml # wait", "l 102"].join("\n");
  assert.strictEqual(
    formatPlan(src),
    ["l 101                         # land it", "w deploy.yaml                 # wait", "l 102", ""].join("\n")
  );
});

test("widens the column for an l or w step that reaches into it", () => {
  // The whole block moves, so its comments still line up with each other.
  const src = ["w a-really-long-workflow-name.yaml # deploy", "l 102 # next"].join("\n");
  assert.strictEqual(
    formatPlan(src),
    ["w a-really-long-workflow-name.yaml    # deploy", "l 102                                 # next", ""].join("\n")
  );
});

test("a c step never sets the column", () => {
  // A condition is free text that routinely runs long; letting it set the
  // column would drag every comment in the block off to the right.
  const src = ["c xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx # sign off", "l 101 # land it"].join("\n");
  assert.strictEqual(
    formatPlan(src),
    ["c xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx # sign off", "l 101                         # land it", ""].join("\n")
  );
});

test("a c step past the column keeps a space before its comment", () => {
  // Without the space the '#' would become part of the condition, which would
  // change what the plan does.
  const formatted = formatPlan("c xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx # sign off\n");
  assert.strictEqual(formatted, "c xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx # sign off\n");
});

test("aligns each block of contiguous lines on its own", () => {
  const src = ["l 101 # a", "", "w a-really-long-workflow-name.yaml # b"].join("\n");
  assert.strictEqual(
    formatPlan(src),
    ["l 101                         # a", "", "w a-really-long-workflow-name.yaml    # b", ""].join("\n")
  );
});

test("whole-line comments keep their place inside a block", () => {
  const src = ["l 101 # a", "  # a note", "l 102 # b"].join("\n");
  assert.strictEqual(
    formatPlan(src),
    ["l 101                         # a", "  # a note", "l 102                         # b", ""].join("\n")
  );
});

test("collapses the space after a step keyword but not inside its argument", () => {
  const src = ["l    101", "c   ship  it  now", "   w   deploy.yaml"].join("\n");
  assert.strictEqual(
    formatPlan(src),
    ["l 101", "c ship  it  now", "w deploy.yaml", ""].join("\n")
  );
});

test("leaves a '#' that is not a comment alone", () => {
  // The parser only cuts a step at ' #', so these are not comments and the
  // formatter must not align (or strip) them.
  const src = ["c ship it#now", "l #123", "l 101\t# tabbed"].join("\n");
  assert.strictEqual(
    formatPlan(src),
    ["c ship it#now", "l                             #123", "l 101\t# tabbed", ""].join("\n")
  );
});

test("passes an unrecognized line through untouched", () => {
  const src = ["land 101 # who knows", "l 102 # ok"].join("\n");
  assert.strictEqual(
    formatPlan(src),
    ["land 101                      # who knows", "l 102                         # ok", ""].join("\n")
  );
});

test("normalizes trailing whitespace and the final newline", () => {
  const src = "l 101   \n\t\nl 102\n\n\n";
  assert.strictEqual(formatPlan(src), "l 101\n\nl 102\n");
});

test("preserves CRLF line endings", () => {
  assert.strictEqual(
    formatPlan("l 101 # a\r\nl 102\r\n"),
    "l 101                         # a\r\nl 102\r\n"
  );
});

test("is idempotent", () => {
  const src = [
    "# Autoland plan — edit steps below.",
    "#",
    "l 101 # Add /widgets API endpoint",
    "w deploy.yaml # wait for the deploy",
    "c QA sign-off complete",
    "",
    "l 102",
    "l https://github.com/o/r/pull/103 # docs",
  ].join("\n");
  const once = formatPlan(src);
  assert.strictEqual(formatPlan(once), once);
});

test("the shipped example plan is already formatted", () => {
  // The example is the reference for the syntax, so it should also be the
  // reference for the layout the formatter produces.
  const example = fs.readFileSync(
    path.join(__dirname, "..", "examples", "example.autoland-plan"),
    "utf8"
  );
  assert.strictEqual(formatPlan(example), example);
});
