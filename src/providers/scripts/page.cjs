"use strict";
const { connect } = require("./connection.cjs");
const { renderMarkdown } = require("./markdown.cjs");

async function openPage(input, origin, path = "/", onTab = () => {}) {
  const base = new URL(input.cdp_url);
  if (
    base.protocol !== "http:" ||
    !["127.0.0.1", "localhost", "[::1]"].includes(base.hostname) ||
    base.username ||
    base.password
  )
    throw Error("Local debugging browser required");
  const json = async (url) => {
    const response = await fetch(new URL(url, base), {
      signal: AbortSignal.timeout(5000),
    });
    if (!response.ok) throw Error("Connected browser unavailable");
    return response.json();
  };
  const tabs = await json("/json/list");
  let tab = tabs.find(
    (t) =>
      t.id === input.tab_id &&
      t.type === "page" &&
      new URL(t.url).origin === origin,
  );
  if (input.mode === "close") {
    if (tab)
      await fetch(new URL("/json/close/" + tab.id, base), {
        signal: AbortSignal.timeout(5000),
      });
    return null;
  }
  if (!tab) {
    const meta = await json("/json/version");
    const browser = await connect(meta.webSocketDebuggerUrl, base);
    let created;
    try {
      created = await browser.call("Target.createTarget", {
        url: origin + path,
        background: input.mode !== "login",
      });
    } finally {
      browser.close();
    }
    tab = (await json("/json/list")).find((t) => t.id === created.targetId);
  }
  if (!tab) throw Error("Provider tab unavailable");
  onTab(tab.id);
  const cdp = await connect(tab.webSocketDebuggerUrl, base);
  const evaluate = async (fn, ...args) => {
    const result = await cdp.call(
      "Runtime.evaluate",
      {
        expression: `(()=>{const renderMarkdown=${renderMarkdown.toString()};return (${fn.toString()})(${args.map((x) => (x === undefined ? "undefined" : JSON.stringify(x))).join(",")});})()`,
        awaitPromise: true,
        returnByValue: true,
        userGesture: true,
      },
      40000,
    );
    if (result.exceptionDetails)
      throw Error(
        result.exceptionDetails.exception?.description?.split("\n")[0] ||
          "Provider page operation failed",
      );
    return result.result.value;
  };
  await cdp.call("Emulation.setFocusEmulationEnabled", { enabled: true });
  return {
    id: tab.id,
    cdp,
    evaluate,
    close: async () => {
      await cdp
        .call("Emulation.setFocusEmulationEnabled", { enabled: false })
        .catch(() => {});
      cdp.close();
    },
  };
}
module.exports = { openPage };

if (require.main === module) {
  const fs = require("node:fs");
  const input = JSON.parse(fs.readFileSync(0, "utf8"));
  if (
    ![
      "https://chatgpt.com/",
      "https://gemini.google.com/",
      "https://claude.ai/",
    ].includes(input.origin)
  )
    throw Error("Unsupported login origin");
  openPage({ ...input, mode: "login" }, new URL(input.origin).origin)
    .then(async (page) => {
      await page.close();
      process.stdout.write(JSON.stringify({ opened: true }));
    })
    .catch(() => {
      process.stderr.write("Connected browser page could not be opened");
      process.exitCode = 1;
    });
}
