const { test } = require("node:test");
const assert = require("node:assert/strict");
const {
  validate,
  canonicalModel,
} = require("../../src/providers/gemini/scripts/browser.cjs");
test("native Gemini browser transport rejects remote debuggers and foreign notebook identities", () => {
  assert.throws(() =>
    validate({ mode: "send", cdp_url: "http://example.com:9231" }),
  );
  assert.throws(() =>
    validate({
      mode: "send",
      cdp_url: "http://127.0.0.1:9231",
      project_id: "https://other/notebook/abcdef",
    }),
  );
  assert.throws(() =>
    validate({ mode: "send", cdp_url: "http://user:secret@127.0.0.1:9231" }),
  );
  assert.throws(() =>
    validate({
      mode: "send",
      cdp_url: "http://127.0.0.1:9231",
      conversation_id: "../escape",
    }),
  );
  assert.equal(
    validate({
      mode: "send",
      cdp_url: "http://127.0.0.1:9231",
      project_id: "notebooks/abcdef",
    }).port,
    "9231",
  );
});
test("native model aliases are stable while displayed versions remain dynamic", () => {
  assert.equal(canonicalModel("3.8 Flash"), "gemini-flash");
  assert.equal(canonicalModel("Flash\nOrta"), "gemini-flash");
  assert.equal(canonicalModel("9.2 Flash-Lite"), "gemini-flash-lite");
  assert.equal(canonicalModel("12 New Model"), "gemini-new-model");
});
const {
  renderMarkdown,
} = require("../../src/providers/gemini/scripts/markdown.cjs");
function text(value) {
  return { nodeType: 3, textContent: value };
}
function element(tag, children = [], attrs = {}) {
  return {
    nodeType: 1,
    tagName: tag,
    childNodes: children,
    getAttribute: (key) => attrs[key] ?? null,
    querySelector: () => null,
    textContent: children.map((x) => x.textContent || "").join(""),
  };
}
test("native responses preserve Markdown, source links and code instead of flattening text", () => {
  const root = element("DIV", [
    element("H2", [text("Title")]),
    element("P", [
      element("STRONG", [text("Bold")]),
      text(" and "),
      element("A", [text("Source")], { href: "https://example.com/source" }),
    ]),
    element("PRE", [text("print(1)")]),
    element("UL", [element("LI", [text("Item")])]),
  ]);
  const result = renderMarkdown(root);
  assert.match(result, /## Title/);
  assert.match(result, /\*\*Bold\*\*/);
  assert.match(result, /\[Source\]\(https:\/\/example.com\/source\)/);
  assert.ok(result.includes("```\nprint(1)\n```"));
  assert.ok(result.includes("- Item"));
  assert.equal(
    renderMarkdown(
      element("A", [text("safe")], { href: "javascript:alert(1)" }),
    ),
    "safe",
  );
  assert.equal(renderMarkdown(element("BUTTON", [text("Copy")])), "");
});
