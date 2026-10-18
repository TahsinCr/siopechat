"use strict";
const fs = require("node:fs");
const { openPage } = require("./page.cjs");
const emit = (data) => process.stdout.write(JSON.stringify(data) + "\n");
const pause = (ms) => new Promise((r) => setTimeout(r, ms));

async function projectUI(c, origin, path) {
  const p = await openPage(c, origin, path, (id) => emit({ type: "tab", id }));
  const wait = async (read, label, timeout = 12000) => {
    const end = Date.now() + timeout;
    while (Date.now() < end) {
      const value = await read();
      if (value) return value;
      await pause(100);
    }
    throw Error("Proje bileşeni hazır değil: " + label);
  };
  const click = async (pattern, root = "body", dom = false) => {
    if (dom) {
      await wait(
        () =>
          p.evaluate(
            (pattern, root) => {
              const es = [
                ...document.querySelectorAll(
                  root +
                    " button," +
                    root +
                    " a," +
                    root +
                    " [role=button]," +
                    root +
                    " [role=menuitem]",
                ),
              ].sort(
                (a, b) =>
                  (b.getAttribute("role") === "menuitem") -
                  (a.getAttribute("role") === "menuitem"),
              );
              const e = es.find(
                (e) =>
                  e.getClientRects().length &&
                  e.checkVisibility?.({
                    checkOpacity: true,
                    checkVisibilityCSS: true,
                  }) !== false &&
                  !e.disabled &&
                  new RegExp(pattern, "i").test(
                    (
                      e.getAttribute("aria-label") ||
                      e.textContent ||
                      ""
                    ).trim(),
                  ),
              );
              if (!e) return false;
              (e.querySelector(".label") || e).click();
              return true;
            },
            pattern,
            root,
          ),
        pattern,
      );
      await pause(350);
      return;
    }
    const xy = await wait(
      () =>
        p.evaluate(
          (pattern, root) => {
            const re = new RegExp(pattern, "i");
            const e = [
              ...document.querySelectorAll(
                root +
                  " button," +
                  root +
                  " a," +
                  root +
                  " [role=button]," +
                  root +
                  " [role=menuitem]," +
                  root +
                  " [role=tab]," +
                  root +
                  " [role=radio]",
              ),
            ]
              .sort(
                (a, b) =>
                  (b.getAttribute("role") === "menuitem") -
                  (a.getAttribute("role") === "menuitem"),
              )
              .find(
                (e) =>
                  e.getClientRects().length &&
                  !e.disabled &&
                  re.test(
                    (
                      e.getAttribute("aria-label") ||
                      e.textContent ||
                      ""
                    ).trim(),
                  ),
              );
            if (!e) return null;
            e.scrollIntoView({ block: "nearest" });
            const r = e.getBoundingClientRect();
            return { x: r.x + r.width / 2, y: r.y + r.height / 2 };
          },
          pattern,
          root,
        ),
      pattern,
    );
    await p.cdp.call("Input.dispatchMouseEvent", { type: "mouseMoved", ...xy });
    await pause(80);
    await p.cdp.call("Input.dispatchMouseEvent", {
      type: "mousePressed",
      ...xy,
      button: "left",
      clickCount: 1,
    });
    await p.cdp.call("Input.dispatchMouseEvent", {
      type: "mouseReleased",
      ...xy,
      button: "left",
      clickCount: 1,
    });
    await pause(350);
  };
  const has = (pattern, root = "body") =>
    p.evaluate(
      (pattern, root) =>
        [
          ...document.querySelectorAll(
            root +
              " button," +
              root +
              " [role=menuitem]," +
              root +
              " [role=radio]," +
              root +
              " [role=option]",
          ),
        ].some(
          (e) =>
            e.getClientRects().length &&
            !e.disabled &&
            new RegExp(pattern, "i").test(
              (e.getAttribute("aria-label") || e.textContent || "").trim(),
            ),
        ),
      pattern,
      root,
    );
  const fill = async (selector, value) => {
    await wait(
      () =>
        p.evaluate(
          (s) =>
            [...document.querySelectorAll(s)].some(
              (e) => e.getClientRects().length && !e.disabled,
            ),
          selector,
        ),
      selector,
    );
    await p.evaluate((selector) => {
      const e = [...document.querySelectorAll(selector)].find(
        (e) => e.getClientRects().length && !e.disabled,
      );
      e.focus();
    }, selector);
    await pause(100);
    const focused = await p.evaluate(
      (s) => document.activeElement?.matches(s),
      selector,
    );
    if (!focused)
      throw Error("Proje alanı odağı kayboldu; değişiklik uygulanmadı");
    await p.cdp.call("Input.dispatchKeyEvent", {
      type: "keyDown",
      key: "a",
      code: "KeyA",
      windowsVirtualKeyCode: 65,
      modifiers: 2,
      commands: ["selectAll"],
    });
    await p.cdp.call("Input.dispatchKeyEvent", {
      type: "keyUp",
      key: "a",
      code: "KeyA",
      windowsVirtualKeyCode: 65,
      modifiers: 0,
    });
    if (value) await p.cdp.call("Input.insertText", { text: value });
    else {
      await p.cdp.call("Input.dispatchKeyEvent", {
        type: "keyDown",
        key: "Backspace",
        code: "Backspace",
        windowsVirtualKeyCode: 8,
      });
      await p.cdp.call("Input.dispatchKeyEvent", {
        type: "keyUp",
        key: "Backspace",
        code: "Backspace",
        windowsVirtualKeyCode: 8,
      });
    }
    await p.evaluate(() => document.activeElement?.blur());
  };
  const escape = async () => {
    await p.cdp.call("Input.dispatchKeyEvent", {
      type: "keyDown",
      key: "Escape",
      code: "Escape",
      windowsVirtualKeyCode: 27,
    });
    await p.cdp.call("Input.dispatchKeyEvent", {
      type: "keyUp",
      key: "Escape",
      code: "Escape",
      windowsVirtualKeyCode: 27,
    });
    await pause(200);
  };
  const chooseFile = async (pattern, path = null) => {
    await p.cdp.call("Page.enable");
    await p.cdp.call("Page.setInterceptFileChooserDialog", { enabled: true });
    let node;
    p.cdp.on("Page.fileChooserOpened", (event) => {
      node = event.backendNodeId;
    });
    try {
      await click(pattern);
      await wait(() => Promise.resolve(node), "source upload control", 5000);
      const detail = await p.cdp.call("DOM.describeNode", {
        backendNodeId: node,
      });
      const attrs = detail.node.attributes || [],
        i = attrs.indexOf("accept");
      await p.cdp.call("DOM.setFileInputFiles", {
        backendNodeId: node,
        files: path ? [path] : [],
      });
      return i >= 0 ? attrs[i + 1] : "";
    } finally {
      await p.cdp.call("Page.setInterceptFileChooserDialog", {
        enabled: false,
      });
    }
  };
  if (c.mode !== "close") {
    await p.cdp.call("Page.bringToFront");
    const navigation = await p.cdp.call("Page.navigate", {
      url: origin + path,
    });
    if (navigation.errorText)
      throw Error("Project navigation failed: " + navigation.errorText);
    await wait(
      () =>
        p.evaluate(
          (origin, path) =>
            location.origin === origin &&
            location.pathname.replace(/\/$/, "") === path.replace(/\/$/, "") &&
            document.readyState !== "loading",
          origin,
          path,
        ),
      "navigation",
      30000,
    );
  }
  return { p, wait, click, fill, has, escape, chooseFile, pause };
}

function main(handler) {
  const raw = fs.readFileSync(0, "utf8");
  if (raw.length > 2 * 1024 * 1024) throw Error("Project input too large");
  const c = JSON.parse(raw);
  if (
    ![
      "options",
      "create",
      "update",
      "add_source",
      "remove_source",
      "close",
    ].includes(c.mode)
  )
    throw Error("Invalid project operation");
  handler(c)
    .then((data) => emit({ type: "result", data }))
    .catch((e) => {
      emit({ type: "error", message: e.message, status: e.status });
      process.exitCode = 1;
    });
}
module.exports = { projectUI, main };
