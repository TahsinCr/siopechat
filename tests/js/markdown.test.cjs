const { test } = require("node:test");
const assert = require("node:assert/strict");
const {
  renderMarkdown,
} = require("../../src/providers/scripts/markdown.cjs");
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
