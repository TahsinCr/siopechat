"use strict";
function modelOptions() {
  return Array.from(
    document.querySelectorAll("[data-model-id][role=menuitemradio]"),
  )
    .filter(
      (e) =>
        e.getClientRects().length &&
        e.checkVisibility?.({
          checkOpacity: true,
          checkVisibilityCSS: true,
        }) !== false &&
        !e.hasAttribute("data-disabled") &&
        e.getAttribute("aria-disabled") !== "true",
    )
    .map((e) => ({
      id: e.dataset.modelId,
      title: e.innerText.split("\n")[0],
      selected: e.getAttribute("aria-checked") === "true",
    }));
}

function effortOptions() {
  const visible = (e) =>
    e.getClientRects().length &&
    e.checkVisibility?.({ checkOpacity: true, checkVisibilityCSS: true }) !==
      false;
  const levels = Array.from(document.querySelectorAll("[data-effort-id]"))
    .filter(
      (e) =>
        visible(e) &&
        e.getAttribute("aria-disabled") !== "true" &&
        !e.hasAttribute("data-disabled"),
    )
    .map((e) => ({
      id: e.dataset.effortId,
      title: e.innerText.split("\n")[0],
      selected: e.getAttribute("aria-checked") === "true",
    }));
  if (levels.length) return levels;
  const toggle = Array.from(document.querySelectorAll("[role=switch]")).find(
    (e) =>
      visible(e) && e.closest("[role=menuitemcheckbox],[role=menuitemradio]"),
  );
  if (!toggle) return [];
  const row = toggle.closest("[role=menuitemcheckbox],[role=menuitemradio]");
  const title = row.innerText.split("\n")[0].trim();
  const enabled = toggle.getAttribute("aria-checked") === "true";
  const fixed =
    toggle.disabled ||
    toggle.getAttribute("aria-disabled") === "true" ||
    toggle.hasAttribute("disabled");
  return [true, false]
    .filter((value) => !fixed || value === enabled)
    .map((value) => ({
      id: "thinking:" + (value ? "on" : "off"),
      title: value ? title : title + " (kapalı)",
      selected: value === enabled,
    }));
}

function catalogControls(p, wait, pause) {
  const navigate = async (path) => {
    const navigation = await p.cdp.call("Page.navigate", {
      url: "https://claude.ai" + path,
    });
    await wait(async () => {
      const tree = await p.cdp.call("Page.getFrameTree");
      if (
        navigation.loaderId &&
        tree.frameTree.frame.loaderId !== navigation.loaderId
      )
        return false;
      return p.evaluate(
        (path) =>
          location.pathname === path &&
          document.readyState === "complete" &&
          !!document.querySelector("[data-testid=model-selector-dropdown]"),
        path,
      );
    }, "composer");
  };
  const menu = async () => {
    if ((await p.evaluate(modelOptions)).length) return;
    await wait(
      () =>
        p.evaluate(
          () =>
            !!document.querySelector("[data-testid=model-selector-dropdown]"),
        ),
      "model selector",
    );
    await p.evaluate(() =>
      document.querySelector("[data-testid=model-selector-dropdown]").click(),
    );
    await wait(
      async () => (await p.evaluate(modelOptions)).length,
      "model menu",
    );
  };
  const openSubmenu = async (pattern) => {
    const focused = await p.evaluate((pattern) => {
      const re = new RegExp(pattern, "i");
      const e = Array.from(
        document.querySelectorAll("[role=menuitem][aria-haspopup=menu]"),
      ).find(
        (e) =>
          e.getClientRects().length &&
          e.checkVisibility?.({
            checkOpacity: true,
            checkVisibilityCSS: true,
          }) !== false &&
          re.test(e.innerText),
      );
      if (
        !e ||
        e.getAttribute("aria-disabled") === "true" ||
        e.hasAttribute("data-disabled")
      )
        return false;
      e.focus();
      return true;
    }, pattern);
    if (!focused) return false;
    for (const type of ["keyDown", "keyUp"])
      await p.cdp.call("Input.dispatchKeyEvent", {
        type,
        key: "ArrowRight",
        code: "ArrowRight",
        windowsVirtualKeyCode: 39,
      });
    await pause(250);
    return true;
  };
  const more = async () => {
    await openSubmenu("^(More models|Daha fazla model)");
  };
  const select = async (id) => {
    await menu();
    if (!(await p.evaluate(modelOptions)).some((x) => x.id === id))
      await more();
    const choice = (await p.evaluate(modelOptions)).find((x) => x.id === id);
    if (!choice)
      throw Error("Selected Claude model unavailable; synchronize models");
    await p.evaluate(
      (id) =>
        Array.from(
          document.querySelectorAll("[data-model-id][role=menuitemradio]"),
        )
          .find((e) => e.dataset.modelId === id)
          .click(),
      id,
    );
    await wait(
      () =>
        p.evaluate(
          (title) =>
            document
              .querySelector("[data-testid=model-selector-dropdown]")
              ?.innerText.trim()
              .startsWith(title),
          choice.title,
        ),
      "model selection",
      5,
    );
    await dismiss();
  };
  const effortMenu = async () => {
    await menu();
    const opened = await openSubmenu("^(Effort|Efor)");
    if (!opened) return [];
    await pause(150);
    await wait(
      async () => (await p.evaluate(effortOptions)).length,
      "effort menu",
      2,
    );
    return p.evaluate(effortOptions);
  };
  const dismiss = async () => {
    for (let i = 0; i < 3; i++) {
      for (const type of ["keyDown", "keyUp"])
        await p.cdp.call("Input.dispatchKeyEvent", {
          type,
          key: "Escape",
          code: "Escape",
          windowsVirtualKeyCode: 27,
        });
      await pause(50);
    }
    await wait(
      async () =>
        !(await p.evaluate(modelOptions)).length &&
        !(await p.evaluate(effortOptions)).length,
      "menu close",
      5,
    );
  };
  async function discover() {
    await navigate("/new");
    await menu();
    let options = await p.evaluate(modelOptions);
    const initial = options.find((x) => x.selected);
    await more();
    options = Array.from(
      new Map(
        [...options, ...(await p.evaluate(modelOptions))].map((x) => [x.id, x]),
      ).values(),
    );
    await dismiss();
    await dismiss();
    const result = [];
    try {
      for (const model of options) {
        try {
          await select(model.id);
        } catch (error) {
          result.push({
            ...model,
            efforts: [],
            selection_error: error.message,
          });
          await dismiss().catch(() => {});
          continue;
        }
        try {
          result.push({ ...model, efforts: await effortMenu() });
        } catch (error) {
          result.push({ ...model, efforts: [], effort_error: error.message });
        }
        await dismiss().catch(() => {});
      }
    } finally {
      if (initial) await select(initial.id).catch(() => {});
      await dismiss().catch(() => {});
    }
    return { models: result, default_model: initial?.id };
  }
  return { navigate, select, effortMenu, dismiss, discover };
}
module.exports = { modelOptions, effortOptions, catalogControls };
