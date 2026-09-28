"use strict";

const test = require("node:test");
const assert = require("node:assert");
const Module = require("node:module");

// src/extension.js is the only file that talks to VS Code, so it is exercised
// against a stub: a wrong language id here would leave the formatter silently
// unregistered, which no amount of testing src/format.js would catch.
function activateWithStub() {
  const registered = [];
  const stub = {
    languages: {
      registerDocumentFormattingEditProvider(selector, provider) {
        registered.push({ selector, provider });
        return { dispose() {} };
      },
    },
    Range: class {
      constructor(start, end) {
        this.start = start;
        this.end = end;
      }
    },
    TextEdit: {
      replace: (range, newText) => ({ range, newText }),
    },
  };

  const load = Module._load;
  Module._load = (request, parent, isMain) =>
    request === "vscode" ? stub : load(request, parent, isMain);
  try {
    delete require.cache[require.resolve("../src/extension")];
    const extension = require("../src/extension");
    const context = { subscriptions: [] };
    extension.activate(context);
    assert.strictEqual(context.subscriptions.length, 1);
    return registered;
  } finally {
    Module._load = load;
    delete require.cache[require.resolve("../src/extension")];
  }
}

function fakeDocument(text) {
  return { getText: () => text, positionAt: (offset) => offset };
}

test("registers a formatter for the plan language", () => {
  const [registration] = activateWithStub();
  assert.strictEqual(registration.selector, "autoland-plan");

  const edits = registration.provider.provideDocumentFormattingEdits(
    fakeDocument("l 101 # a\n")
  );
  assert.deepStrictEqual(
    edits.map((edit) => edit.newText),
    ["l 101                         # a\n"]
  );
  // ...replacing the whole document, not a slice of it.
  assert.strictEqual(edits[0].range.start, 0);
  assert.strictEqual(edits[0].range.end, "l 101 # a\n".length);
});

test("edits nothing when the document is already formatted", () => {
  const [registration] = activateWithStub();
  const edits = registration.provider.provideDocumentFormattingEdits(
    fakeDocument("l 101                         # a\n")
  );
  assert.deepStrictEqual(edits, []);
});
