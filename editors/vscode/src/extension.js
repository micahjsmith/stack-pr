"use strict";

const vscode = require("vscode");
const { formatPlan } = require("./format");

function activate(context) {
  context.subscriptions.push(
    vscode.languages.registerDocumentFormattingEditProvider("autoland-plan", {
      provideDocumentFormattingEdits(document) {
        const text = document.getText();
        const formatted = formatPlan(text);
        if (formatted === text) {
          return [];
        }
        const whole = new vscode.Range(
          document.positionAt(0),
          document.positionAt(text.length)
        );
        return [vscode.TextEdit.replace(whole, formatted)];
      },
    })
  );
}

function deactivate() {}

module.exports = { activate, deactivate };
