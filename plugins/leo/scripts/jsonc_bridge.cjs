#!/usr/bin/env node
"use strict";
const parser = require(process.argv[2]);
try {
  const request = JSON.parse(require("fs").readFileSync(0, "utf8"));
  const errors = [];
  const value = parser.parse(request.text, errors, { allowTrailingComma: true, disallowComments: false });
  if (errors.length) throw new Error("invalid JSONC (parse error " + errors[0].error + ")");
  if (request.mode === "parse") process.stdout.write(JSON.stringify({ value }));
  else {
    let text = request.text;
    const eol = text.includes("\r\n") ? "\r\n" : "\n";
    const indentation = text.match(/(?:\r?\n)([ \t]+)"/);
    const indent = indentation ? indentation[1] : "  ";
    for (const edit of request.edits || []) {
      text = parser.applyEdits(text, parser.modify(text, edit.path, edit.value, {
        isArrayInsertion: false,
        formattingOptions: { insertSpaces: !indent.includes("\t"), tabSize: indent.includes("\t") ? 1 : Math.max(1, indent.length), eol, keepLines: true },
      }));
    }
    process.stdout.write(JSON.stringify({ text }));
  }
} catch (error) { process.stderr.write(String(error && error.message || error)); process.exit(2); }
