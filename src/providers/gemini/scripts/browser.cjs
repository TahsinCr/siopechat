"use strict";
const fs = require("node:fs");
const { connect } = require("../../scripts/connection.cjs");
const { renderMarkdown } = require("./markdown.cjs");

function validate(input) {
  const url = new URL(input.cdp_url);
  if (
    url.protocol !== "http:" ||
    !["127.0.0.1", "localhost", "[::1]"].includes(url.hostname) ||
    url.username ||
    url.password
  )
    throw Error("Only a local debugging browser is supported");
  if (!["models", "send", "delete_chat", "close"].includes(input.mode))
    throw Error("Unknown browser operation");
  if (
    input.project_id &&
    !/^notebooks\/[A-Za-z0-9_-]{6,}$/.test(input.project_id)
  )
    throw Error("Invalid notebook identity");
  if (input.conversation_id && !/^c_[a-f0-9]+$/.test(input.conversation_id))
    throw Error("Invalid conversation identity");
  if (input.mode === "delete_chat" && !input.conversation_id)
    throw Error("Conversation identity required");
  return url;
}
function canonicalModel(title) {
  return (
    "gemini-" +
    title
      .split("\n")[0]
      .replace(/^\d+(?:\.\d+)*\s+/, "")
      .trim()
      .toLowerCase()
      .replace(/\s+/g, "-")
  );
}
function emit(value) {
  process.stdout.write(JSON.stringify(value) + "\n");
}
const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
// Executed only inside our own background tab, never in the user's open chat.
function pageState() {
  const visible = (e) => !!e.getClientRects().length;
  const editor = document.querySelector('[contenteditable][role="textbox"]');
  const responses = Array.from(document.querySelectorAll("model-response"));
  const last = responses.at(-1);
  const markdown = last?.querySelector(".markdown");
  const thinking = last?.querySelector(
    "thoughts-content,.thoughts-content,.thoughts-container",
  );
  const buttons = Array.from(document.querySelectorAll("button")).filter(
    visible,
  );
  const stop = buttons.find(
    (e) =>
      e.classList.contains("stop-button") ||
      /stop|durdur|yanıtı durdur/i.test(e.getAttribute("aria-label") || ""),
  );
  return {
    ready: !!editor && !!window.WIZ_global_data?.SNlM0e,
    count: responses.length,
    text: markdown ? renderMarkdown(markdown) : "",
    thoughts: thinking ? renderMarkdown(thinking) : "",
    busy: !!stop,
    path: location.pathname,
    promptCount: document.querySelectorAll("user-query").length,
    temporary: !!document.querySelector(".temp-chat-on"),
  };
}
function menuModels() {
  return Array.from(
    document.querySelectorAll('[data-test-id^="bard-mode-option-"]'),
  )
    .filter((e) => e.getClientRects().length)
    .map((e) => ({
      title:
        e.querySelector(".label")?.textContent.trim() ||
        e.innerText.split("\n")[0].trim(),
      remote: e.getAttribute("data-test-id").slice("bard-mode-option-".length),
      selected: e.classList.contains("selected"),
    }));
}
function menuEfforts(model) {
  const items = Array.from(document.querySelectorAll("[role=menuitem]")).filter(
    (e) => e.getClientRects().length && !e.hasAttribute("data-test-id"),
  );
  return items.flatMap((e) => {
    const log = e.getAttribute("jslog") || "",
      token = log.match(/BardVeMetadataKey:([^;]+)/)?.[1];
    if (!token) return [];
    let tree;
    try {
      tree = JSON.parse(atob(token));
    } catch {
      return [];
    }
    function find(x) {
      if (!Array.isArray(x)) return null;
      if (x[0] === model && Number.isInteger(x[1])) return x[1];
      for (const v of x) {
        const r = find(v);
        if (r !== null) return r;
      }
      return null;
    }
    const code = find(tree);
    return code === null
      ? []
      : [
          {
            id: "native:" + code,
            title:
              e.querySelector(".label")?.textContent.trim() ||
              e.innerText.split("\n")[0].trim(),
            selected: e.classList.contains("selected"),
          },
        ];
  });
}
async function deleteChatInPage(cid) {
  if (location.origin !== "https://gemini.google.com" || !/^c_[a-f0-9]+$/.test(cid))
    throw Error("Invalid Gemini deletion context");
  const token = window.WIZ_global_data?.SNlM0e;
  if (!token) throw Error("Gemini sign-in required");
  const rpcid = "GzXR5e";
  const url = new URL("/_/BardChatUi/data/batchexecute", location.origin);
  url.searchParams.set("rpcids", rpcid);
  url.searchParams.set("source-path", "/app");
  const response = await fetch(url, {
    method: "POST",
    credentials: "same-origin",
    headers: { "content-type": "application/x-www-form-urlencoded;charset=UTF-8", "x-same-domain": "1" },
    body: new URLSearchParams({ at: token, "f.req": JSON.stringify([[[rpcid, JSON.stringify([cid]), null, "generic"]]]) }),
    signal: AbortSignal.timeout(15000),
  });
  if (!response.ok) throw Error("Gemini conversation deletion failed");
  const body = await response.text();
  for (const line of body.split("\n")) {
    if (!line.startsWith('[["wrb.fr"')) continue;
    for (const part of JSON.parse(line)) {
      if (part[0] !== "wrb.fr" || part[1] !== rpcid) continue;
      if (part[2] == null) throw Error("Gemini deletion was not confirmed");
      JSON.parse(part[2]);
      return true;
    }
  }
  throw Error("Gemini deletion response changed; refresh before retrying");
}
async function run(input) {
  const base = validate(input);
  const json = async (path, options) => {
    const r = await fetch(new URL(path, base), {
      ...options,
      signal: AbortSignal.timeout(5000),
    });
    if (!r.ok) throw Error("Debugging browser unavailable");
    return r.json();
  };
  let tab;
  if (input.tab_id)
    tab = (await json("/json/list")).find((t) => t.id === input.tab_id);
  if (input.mode === "close") {
    if (tab) await fetch(new URL("/json/close/" + tab.id, base));
    return;
  }
  const route = input.mode === "delete_chat" ? "/app" : input.conversation_id
    ? "/app/" + input.conversation_id.slice(2)
    : input.project_id
      ? "/notebook/" + input.project_id.slice(10)
      : "/app";
  if (!tab && input.resume_temporary)
    throw Error("Temporary Gemini browser context was closed");
  if (!tab) {
    const version = await json("/json/version");
    const browser = await connect(version.webSocketDebuggerUrl, base);
    try {
      const created = await browser.call("Target.createTarget", {
        url: "https://gemini.google.com" + route,
        background: true,
      });
      tab = (await json("/json/list")).find((t) => t.id === created.targetId);
    } finally {
      browser.close();
    }
  }
  if (!tab || !tab.url.startsWith("https://gemini.google.com/"))
    throw Error("Gemini browser tab unavailable");
  emit({ type: "tab", id: tab.id });
  const cdp = await connect(tab.webSocketDebuggerUrl, base);
  await cdp.call("Emulation.setFocusEmulationEnabled", { enabled: true });
  const evaluate = async (expression) => {
    const r = await cdp.call("Runtime.evaluate", {
      expression,
      returnByValue: true,
      awaitPromise: true,
      userGesture: true,
    });
    if (r.exceptionDetails) throw Error("Gemini page contract changed");
    return r.result.value;
  };
  const invoke = (fn, ...args) =>
    evaluate(
      "(()=>{const renderMarkdown=" +
        renderMarkdown.toString() +
        ";return (" +
        fn.toString() +
        ")(" +
        args.map((x) => JSON.stringify(x)).join(",") +
        ");})()",
    );
  const wait = async (fn, seconds = 15, label = "page") => {
    const until = Date.now() + seconds * 1000;
    while (Date.now() < until) {
      const value = await fn();
      if (value) return value;
      await pause(200);
    }
    throw Error(
      "Gemini " +
        label +
        " did not become ready; check sign-in in the connected browser",
    );
  };
  let submitted = false;
  const stop = () =>
    invoke(() => {
      Array.from(document.querySelectorAll("button"))
        .find(
          (e) =>
            e.classList.contains("stop-button") ||
            /stop|durdur/i.test(e.getAttribute("aria-label") || ""),
        )
        ?.click();
    }).catch(() => {});
  const cancel = () => {
    stop().finally(async () => {
      await cdp
        .call("Emulation.setFocusEmulationEnabled", { enabled: false })
        .catch(() => {});
      cdp.close();
      process.exit(0);
    });
  };
  process.once("SIGTERM", cancel);
  process.once("SIGINT", cancel);
  try {
    if (input.mode === "send" && !input.resume_temporary)
      await cdp.call("Page.navigate", {
        url: "https://gemini.google.com" + route,
      });
    await pause(200);
    await wait(() =>
      invoke(pageState).then(
        (s) =>
          s.ready &&
          (input.mode !== "send" || input.resume_temporary || s.path === route),
      ),
    );
    if (input.mode === "delete_chat") {
      await invoke(deleteChatInPage, input.conversation_id);
      emit({ type: "deleted", id: input.conversation_id });
      return;
    }
    const openModels = async () => {
      if ((await invoke(menuModels)).length) return;
      await wait(
        () =>
          evaluate(
            '!!document.querySelector("[data-test-id=bard-mode-menu-button]")?.getClientRects().length',
          ),
        30,
        "model selector",
      );
      await evaluate(
        'document.querySelector("[data-test-id=bard-mode-menu-button]").click()',
      );
      await wait(
        () => invoke(menuModels).then((x) => x.length && x),
        10,
        "model menu",
      );
    };
    await openModels();
    const models = await invoke(menuModels);
    if (!models.length) throw Error("Gemini model menu changed");
    const selectModel = async (model) => {
      await openModels();
      await evaluate(
        "document.querySelector(" +
          JSON.stringify(
            '[data-test-id="bard-mode-option-' + model.remote + '"]',
          ) +
          ")?.click()",
      );
      await wait(
        () =>
          evaluate(
            'document.querySelector("[data-test-id=bard-mode-menu-button]")?.innerText.trim().split("\\n")[0] === ' +
              JSON.stringify(model.title.replace(/^\d+(?:\.\d+)*\s+/, "")),
          ),
        10,
        "model selection",
      );
    };
    const efforts = async (model) => {
      await openModels();
      const opened = await invoke(() => {
        const e = Array.from(document.querySelectorAll("[role=menuitem]")).find(
          (e) =>
            e.getClientRects().length &&
            e.getAttribute("aria-haspopup") === "true" &&
            !e.hasAttribute("data-test-id"),
        );
        if (e) {
          e.click();
          return true;
        }
        return false;
      });
      if (!opened) return [];
      for (let i = 0; i < 10; i++) {
        const choices = await invoke(menuEfforts, model.remote);
        if (choices.length) return choices;
        await pause(100);
      }
      return [];
    };
    const escape = async () => {
      await cdp.call("Input.dispatchKeyEvent", {
        type: "keyDown",
        key: "Escape",
        code: "Escape",
        windowsVirtualKeyCode: 27,
      });
      await cdp.call("Input.dispatchKeyEvent", {
        type: "keyUp",
        key: "Escape",
        code: "Escape",
        windowsVirtualKeyCode: 27,
      });
    };
    if (input.mode === "models") {
      const result = [];
      for (const model of models) {
        await selectModel(model);
        const choices = await efforts(model);
        result.push({
          ...model,
          id: canonicalModel(model.title),
          efforts: choices,
        });
        await evaluate(
          'document.querySelector("[data-test-id=bard-mode-menu-button]").click()',
        );
        await pause(100);
      }
      const initial = models.find((m) => m.selected);
      if (initial) await selectModel(initial);
      await escape();
      await escape();
      emit({ type: "models", items: result });
      return;
    }
    const model = models.find(
      (m) =>
        canonicalModel(m.title) === input.model || m.remote === input.model,
    );
    if (input.model && !model)
      throw Error(
        "Selected Gemini model is no longer available; refresh models",
      );
    const selected = model || models.find((m) => m.selected) || models[0];
    await selectModel(selected);
    if (input.effort) {
      const choices = await efforts(selected);
      let wanted = choices.find((x) => x.id === input.effort);
      if (input.effort === "extended") wanted = choices.at(-1);
      if (input.effort === "standard")
        wanted =
          choices.find((x) => x.id === "native:4") ||
          choices.find((x) => x.selected);
      if (!wanted)
        throw Error("Selected Gemini effort is unavailable; refresh models");
      const ok = await invoke(
        (remote, id) => {
          const all = Array.from(
            document.querySelectorAll("[role=menuitem]"),
          ).filter((e) => e.getClientRects().length);
          for (const e of all) {
            const key = e
              .getAttribute("jslog")
              ?.match(/BardVeMetadataKey:([^;]+)/)?.[1];
            if (!key) continue;
            try {
              const tree = JSON.parse(atob(key));
              const walk = (x) =>
                Array.isArray(x) &&
                ((x[0] === remote && "native:" + x[1] === id) || x.some(walk));
              if (walk(tree)) {
                e.click();
                return true;
              }
            } catch {}
          }
          return false;
        },
        selected.remote,
        wanted.id,
      );
      if (!ok) throw Error("Gemini effort control changed");
      await wait(
        () =>
          evaluate(
            'document.querySelector("[data-test-id=bard-mode-menu-button]")?.innerText || ""',
          ).then((text) => text.includes(wanted.title)),
        5,
        "effort selection",
      );
    }
    await escape();
    await escape();
    if (
      input.temporary &&
      !input.resume_temporary &&
      !(await invoke(pageState)).temporary
    ) {
      if (input.project_id)
        throw Error("Gemini does not offer temporary chat inside a notebook");
      const toggled = await invoke(() => {
        const button = document.querySelector(
          "[data-test-id=temp-chat-button-container] button",
        );
        if (button) {
          button.click();
          return true;
        }
        return false;
      });
      if (!toggled) throw Error("Gemini temporary chat control is unavailable");
      await wait(
        () => invoke(pageState).then((s) => s.temporary),
        5,
        "temporary mode",
      );
    }
    if (input.attachments?.length) {
      await invoke(() =>
        Array.from(document.querySelectorAll("button"))
          .find((e) =>
            /upload|yükleme/i.test(e.getAttribute("aria-label") || ""),
          )
          ?.click(),
      );
      await pause(200);
      await invoke(() =>
        Array.from(document.querySelectorAll("[role=menuitem],button"))
          .find(
            (e) =>
              e.getClientRects().length &&
              /upload files|dosya yükle/i.test(e.innerText),
          )
          ?.click(),
      );
      await wait(
        () => evaluate('!!document.querySelector("input[type=file]")'),
        5,
      );
      const doc = await cdp.call("DOM.getDocument");
      const node = await cdp.call("DOM.querySelector", {
        nodeId: doc.root.nodeId,
        selector: "input[type=file]",
      });
      await cdp.call("DOM.setFileInputFiles", {
        nodeId: node.nodeId,
        files: input.attachments,
      });
      await pause(500);
    }
    const before = await invoke(pageState);
    await evaluate(
      'document.querySelector("[contenteditable][role=textbox]").focus()',
    );
    await cdp.call("Input.insertText", { text: input.prompt });
    const sendButton = () =>
      Array.from(document.querySelectorAll("button")).find(
        (e) =>
          e.getClientRects().length &&
          !e.disabled &&
          /send message|mesaj gönder/i.test(e.getAttribute("aria-label") || ""),
      );
    await wait(
      () => evaluate("!!(" + sendButton.toString() + ")()"),
      input.attachments?.length ? 45 : 5,
      "send button",
    );
    const clicked = await evaluate(
      "(" + sendButton.toString() + ")()?.click();true",
    );
    if (!clicked) throw Error("Gemini send control changed");
    await wait(
      () => invoke(pageState).then((s) => s.promptCount > before.promptCount),
      10,
      "send acknowledgement",
    );
    submitted = true;
    emit({ type: "status", stage: "sent" });
    let previous = "",
      thoughts = "",
      stable = 0,
      bound = null;
    const until = Date.now() + 580000;
    while (Date.now() < until) {
      await pause(250);
      const state = await invoke(pageState);
      const remote = state.path.match(/^\/app\/([a-f0-9]+)$/)?.[1];
      if (input.temporary && !bound) {
        bound = input.temporary_id;
        emit({ type: "bound", id: bound });
      }
      if (!input.temporary && remote && "c_" + remote !== bound) {
        bound = "c_" + remote;
        emit({ type: "bound", id: bound });
      }
      if (state.count <= before.count) continue;
      if (state.thoughts !== thoughts) {
        thoughts = state.thoughts;
        emit({ type: "replace", channel: "analysis", text: thoughts });
      }
      if (state.text !== previous) {
        previous = state.text;
        stable = 0;
        emit({ type: "replace", channel: "final", text: previous });
      } else stable++;
      if (previous && !state.busy && stable >= 3) {
        if (!bound)
          throw Error("Gemini did not return a conversation identity");
        emit({ type: "done" });
        return;
      }
    }
    throw Error(
      "Gemini response timed out; check the connected browser before resending",
    );
  } catch (error) {
    if (submitted) await stop();
    throw error;
  } finally {
    process.removeListener("SIGTERM", cancel);
    process.removeListener("SIGINT", cancel);
    await cdp
      .call("Emulation.setFocusEmulationEnabled", { enabled: false })
      .catch(() => {});
    cdp.close();
  }
}
module.exports = { validate, canonicalModel, deleteChatInPage };
if (require.main === module) {
  const raw = fs.readFileSync(0, "utf8");
  if (raw.length > 2 * 1024 * 1024) throw Error("Input too large");
  run(JSON.parse(raw)).catch((error) => {
    emit({ type: "error", message: error.message });
    process.exitCode = 1;
  });
}
