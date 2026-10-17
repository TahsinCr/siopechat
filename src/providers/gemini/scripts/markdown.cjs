"use strict";
// Only text/Markdown leaves the page; page HTML is never executed by the CLI.
function renderMarkdown(root) {
  let visited = 0;
  function walk(node, depth = 0) {
    if (!node || ++visited > 50000 || depth > 40) return "";
    if (node.nodeType === 3) return node.textContent || "";
    const tag = node.tagName;
    if (
      ["BUTTON", "SCRIPT", "STYLE", "SVG"].includes(tag) ||
      node.getAttribute?.("aria-hidden") === "true"
    )
      return "";
    if (tag === "PRE") {
      const code = node.querySelector?.("code") || node;
      const language = code.className?.match(/language-([\w+-]+)/)?.[1] || "";
      const text = code.textContent || "";
      const fence = text.includes("```") ? "````" : "```";
      return `\n\n${fence}${language}\n${text.trimEnd()}\n${fence}\n\n`;
    }
    const text = Array.from(node.childNodes || [])
      .map((n) => walk(n, depth + 1))
      .join("");
    if (tag === "BR") return "\n";
    if (tag === "CODE") return "`" + text + "`";
    if (["STRONG", "B"].includes(tag)) return "**" + text + "**";
    if (["EM", "I"].includes(tag)) return "*" + text + "*";
    if (/^H[1-6]$/.test(tag))
      return "\n\n" + "#".repeat(Number(tag[1])) + " " + text + "\n\n";
    if (tag === "P") return text + "\n\n";
    if (tag === "LI") return "- " + text.trim() + "\n";
    if (["UL", "OL"].includes(tag)) return "\n" + text + "\n";
    if (tag === "BLOCKQUOTE")
      return (
        "\n" +
        text
          .trim()
          .split("\n")
          .map((l) => "> " + l)
          .join("\n") +
        "\n\n"
      );
    if (tag === "A") {
      const url = node.getAttribute("href") || "";
      return /^https?:\/\//i.test(url)
        ? "[" + text + "](" + url.replace(/\)/g, "%29") + ")"
        : text;
    }
    if (tag === "IMG") {
      const url = node.getAttribute("src") || "";
      return /^https?:\/\//i.test(url)
        ? "![" + (node.getAttribute("alt") || "Image") + "](" + url + ")"
        : "";
    }
    if (["TH", "TD"].includes(tag))
      return text.trim().replace(/\|/g, "\\|") + " | ";
    if (tag === "TR") return "| " + text + "\n";
    if (tag === "THEAD")
      return (
        text +
        "| " +
        Array.from(node.querySelectorAll("th"))
          .map(() => "---")
          .join(" | ") +
        " |\n"
      );
    if (tag === "TABLE") return "\n\n" + text + "\n\n";
    return text;
  }
  return walk(root).trim();
}
module.exports = { renderMarkdown };
