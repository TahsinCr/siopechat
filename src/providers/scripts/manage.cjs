"use strict";
const { connect } = require("./connection.cjs");
const fs = require("node:fs");
function validate(c) {
  const b = new URL(c.cdp_url);
  if (
    b.protocol !== "http:" ||
    !["127.0.0.1", "localhost", "[::1]"].includes(b.hostname) ||
    b.username ||
    b.password
  )
    throw Error("Local debugger required");
  if (!["chatgpt", "gemini"].includes(c.provider))
    throw Error("Invalid provider");
  if (
    c.provider === "chatgpt"
      ? !/^g-p-[A-Za-z0-9_-]+$/.test(c.remote)
      : !/^notebooks\/[A-Za-z0-9_-]{6,}$/.test(c.remote)
  )
    throw Error("Invalid project identity");
  return b;
}
function findAction(config, stage, activate = false) {
  const visible = (e) =>
    e.getClientRects().length &&
    !e.disabled &&
    e.checkVisibility({ checkOpacity: true, checkVisibilityCSS: true });
  const label = (e) =>
    (e.getAttribute("aria-label") || e.innerText || "").trim();
  const find = (root, re) =>
    [...root.querySelectorAll("button,[role=menuitem]")].find(
      (e) => visible(e) && re.test(label(e)),
    );
  let target,
    settings = false;
  if (stage === "menu")
    target =
      config.provider === "gemini"
        ? find(document, /^(notebook settings|not defteri ayarları)$/i)
        : find(
            document,
            /^(project actions|project options|proje işlemleri|proje seçenekleri)$/i,
          );
  else if (stage === "delete" || stage === "delete_final") {
    target =
      config.provider === "gemini"
        ? document.querySelector("[data-test-id=project-delete-button]")
        : find(document, /^(delete project|projeyi sil|proje sil)$/i);
    if (!target && stage === "delete" && config.provider === "chatgpt") {
      target = find(document, /^(project settings|proje ayarları)$/i);
      settings = !!target;
    }
  } else if (stage === "confirm") {
    const d = [
      ...document.querySelectorAll(
        "[role=dialog],[role=alertdialog],mat-dialog-container",
      ),
    ]
      .filter(visible)
      .at(-1);
    if (d)
      target = find(
        d,
        /^(delete|delete project|delete notebook|delete everywhere|her yerden sil|delete from chat and work|delete from chat & work|sohbet ve çalışma.dan sil|sil|projeyi sil|not defterini sil)$/i,
      );
  }
  if (!target || !visible(target)) return null;
  if (config.provider !== "gemini" && typeof target.focus === "function")
    target.focus({ preventScroll: true });
  const r = (
    target.querySelector?.(".label") || target
  ).getBoundingClientRect();
  if (
    config.provider !== "gemini" &&
    document.elementFromPoint &&
    !target.contains(
      document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2),
    )
  )
    return null;
  if (activate) (target.querySelector?.(".label") || target).click();
  return { x: r.left + r.width / 2, y: r.top + r.height / 2, settings };
}
async function deleteGeminiInPage() {
  const pause = () => new Promise((r) => setTimeout(r, 100));
  const wait = async (callback) => {
    for (let i = 0; i < 150; i++) {
      const value = callback();
      if (value) return value;
      await pause();
    }
    throw Error("Gemini notebook deletion component unavailable");
  };
  const visible = (e) => e.getClientRects().length && !e.disabled;
  const trigger = await wait(() =>
    [...document.querySelectorAll("button")].find(
      (e) =>
        visible(e) &&
        /^(notebook settings|not defteri ayarları)$/i.test(
          e.getAttribute("aria-label") || "",
        ),
    ),
  );
  trigger.click();
  const item = await wait(() =>
    document.querySelector("[data-test-id=project-delete-button]"),
  );
  await new Promise((r) => setTimeout(r, 500));
  (item.querySelector(".label") || item).click();
  const dialog = await wait(() =>
    [
      ...document.querySelectorAll(
        "mat-dialog-container[role=dialog],[role=alertdialog]",
      ),
    ].find(visible),
  );
  const submit = await wait(() =>
    [...dialog.querySelectorAll("button")].find(
      (e) =>
        visible(e) &&
        /^(delete everywhere|her yerden sil|delete notebook|not defterini sil|delete|sil)$/i.test(
          (e.innerText || e.getAttribute("aria-label") || "").trim(),
        ),
    ),
  );
  submit.click();
  await wait(() => !document.contains(dialog));
  return { submitted: true };
}

async function main(config) {
  const base = validate(config),
    origin =
      config.provider === "gemini"
        ? "https://gemini.google.com"
        : "https://chatgpt.com";
  const route =
    config.provider === "gemini"
      ? "/notebook/" + config.remote.slice(10)
      : "/g/" + config.remote + "/project";
  const json = async (p) => {
    const r = await fetch(new URL(p, base), {
      signal: AbortSignal.timeout(5000),
    });
    if (!r.ok) throw Error("Debugger unavailable");
    return r.json();
  };
  const browser = await connect(
    (await json("/json/version")).webSocketDebuggerUrl,
    base,
  );
  let target, c;
  const cancel = () => {
    (async () => {
      if (c)
        await c
          .call("Page.handleJavaScriptDialog", { accept: false })
          .catch(() => {});
      if (target)
        await browser
          .call("Target.closeTarget", { targetId: target })
          .catch(() => {});
      if (c) c.close();
      browser.close();
      process.exit(0);
    })();
  };
  process.once("SIGTERM", cancel);
  try {
    target = (
      await browser.call("Target.createTarget", {
        url: origin + route,
        background: true,
      })
    ).targetId;
    c = await connect(
      (await json("/json/list")).find((t) => t.id === target)
        .webSocketDebuggerUrl,
      base,
    );
    let armed = false,
      nativeConfirmed = false;
    c.on("Page.javascriptDialogOpening", (event) => {
      // Only our own target and the explicitly armed project deletion may
      // accept a browser-native confirmation; never answer unrelated prompts.
      const accept =
        armed &&
        event.type === "confirm" &&
        /delete|sil|kaldır/i.test(event.message || "");
      c.call("Page.handleJavaScriptDialog", { accept })
        .then(() => {
          nativeConfirmed = accept;
        })
        .catch(() => {});
    });
    await c.call("Page.enable");
    await browser.call("Target.activateTarget", { targetId: target });
    await c.call("Emulation.setFocusEmulationEnabled", { enabled: true });
    const pause = () => new Promise((r) => setTimeout(r, 100));
    const evaluate = async (expression) => {
      const r = await c.call("Runtime.evaluate", {
        expression,
        returnByValue: true,
        userGesture: true,
      });
      return r.exceptionDetails ? null : r.result.value;
    };
    const action = async (stage) => {
      // Overlay enter animations move menu items. Resolve hit-tested positions
      // only after layout settles, rather than clicking a stale bounding box.
      await new Promise((resolve) => setTimeout(resolve, 350));
      for (let i = 0; i < 120; i++) {
        if (stage === "confirm" && nativeConfirmed) return { native: true };
        const ready = await evaluate(
          `location.origin===${JSON.stringify(origin)}&&location.pathname===${JSON.stringify(route)}`,
        ).catch(() => false);
        const b =
          ready &&
          (await evaluate(
            `(${findAction.toString()})(${JSON.stringify(config)},${JSON.stringify(stage)})`,
          ).catch(() => null));
        if (b) return { ...b, stage };
        await pause();
      }
      throw Error(
        "Project deletion " +
          stage +
          " unavailable; check permissions or interface changes",
      );
    };
    const click = async (b) => {
      if (b.native) return;
      if (config.provider === "gemini") {
        await evaluate(
          `(${findAction.toString()})(${JSON.stringify(config)},${JSON.stringify(b.stage)},true)`,
        );
        await pause();
        return;
      }
      // Dialog focus can be reassigned after an overlay mounts. Activate the
      // hit-tested control itself instead of sending Enter to transient focus.
      await c.call("Input.dispatchMouseEvent", {
        type: "mouseMoved",
        x: b.x,
        y: b.y,
      });
      await c.call("Input.dispatchMouseEvent", {
        type: "mousePressed",
        x: b.x,
        y: b.y,
        button: "left",
        clickCount: 1,
      });
      await c.call("Input.dispatchMouseEvent", {
        type: "mouseReleased",
        x: b.x,
        y: b.y,
        button: "left",
        clickCount: 1,
      });
      await pause();
    };
    if (config.provider === "gemini") {
      // Google's custom menu dispatches from its label, not its focusable host.
      for (let i = 0; i < 100; i++) {
        const ready = await evaluate(
          `location.origin===${JSON.stringify(origin)}&&location.pathname===${JSON.stringify(route)}`,
        ).catch(() => false);
        if (ready) break;
        await pause();
      }
      const result = await c.call(
        "Runtime.evaluate",
        {
          expression: `(${deleteGeminiInPage.toString()})()`,
          awaitPromise: true,
          returnByValue: true,
          userGesture: true,
        },
        30000,
      );
      if (result.exceptionDetails)
        throw Error(
          result.exceptionDetails.exception?.description?.split("\n")[0] ||
            "Gemini deletion outcome unknown",
        );
      process.stdout.write(JSON.stringify(result.result.value));
      return;
    }
    await click(await action("menu"));
    let d = await action("delete");
    armed = !d.settings;
    await click(d);
    if (d.settings) {
      const final = await action("delete_final");
      armed = true;
      await click(final);
    }
    await click(await action("confirm"));
    for (let i = 0; i < 100; i++) {
      const hasDialog = await evaluate(
        `!![...document.querySelectorAll('[role=dialog],[role=alertdialog],mat-dialog-container')].filter(e=>e.getClientRects().length).length`,
      );
      if (hasDialog === false) {
        process.stdout.write(JSON.stringify({ submitted: true }));
        return;
      }
      await pause();
    }
    throw Error("Deletion outcome unknown; check before retrying");
  } finally {
    process.removeListener("SIGTERM", cancel);
    if (c) {
      await c
        .call("Emulation.setFocusEmulationEnabled", { enabled: false })
        .catch(() => {});
      c.close();
    }
    if (target)
      await browser
        .call("Target.closeTarget", { targetId: target })
        .catch(() => {});
    browser.close();
  }
}
module.exports = { validate, findAction, deleteGeminiInPage };
if (require.main === module)
  main(JSON.parse(fs.readFileSync(0, "utf8"))).catch((e) => {
    process.stderr.write(e.message);
    process.exitCode = 1;
  });
