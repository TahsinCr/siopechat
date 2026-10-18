"use strict";
const { openPage } = require("../../../scripts/page.cjs");

// Use the website's own creation form. Do not guess private write endpoints.
async function createInPage(config) {
  const pause = () => new Promise((resolve) => setTimeout(resolve, 100));
  const label = (element) =>
    (element.getAttribute("aria-label") || element.textContent || "").trim();
  const buttons = (root) => [
    ...root.querySelectorAll(
      'button,[role="button"],[role="radio"],[role="option"],[role="menuitem"],[role="menuitemradio"]',
    ),
  ];
  const find = (root, pattern) =>
    buttons(root).find(
      (element) => !element.disabled && pattern.test(label(element)),
    );
  const wait = async (callback) => {
    for (let i = 0; i < 120; i++) {
      const value = callback();
      if (value) return value;
      await pause();
    }
    throw Error(
      "Proje oluşturma ekranı bulunamadı; bağlı tarayıcıyı kontrol edin",
    );
  };
  let start = find(
    document,
    /^(new project|add new project|yeni proje|yeni proje ekle)$/i,
  );
  if (!start) {
    const sidebar = find(
      document,
      /^(open sidebar|kenar çubuğunu aç|kenar çubuğunu genişlet)$/i,
    );
    if (sidebar) sidebar.click();
    start = await wait(() =>
      find(
        document,
        /^(new project|add new project|yeni proje|yeni proje ekle)$/i,
      ),
    );
  }
  start.click();
  const dialog = await wait(() =>
    [...document.querySelectorAll('[role="dialog"]')].find((node) =>
      node.querySelector("input"),
    ),
  );
  const input = dialog.querySelector('input[type="text"],input:not([type])');
  if (!input) throw Error("Proje adı alanı değişmiş");
  const setter = Object.getOwnPropertyDescriptor(
    HTMLInputElement.prototype,
    "value",
  ).set;
  setter.call(input, config.name);
  input.dispatchEvent(new Event("input", { bubbles: true }));
  input.dispatchEvent(new Event("change", { bubbles: true }));
  if (config.instructions) {
    const area = dialog.querySelector("textarea");
    if (!area)
      throw Error("Talimat alanı bu oluşturma ekranında desteklenmiyor");
    Object.getOwnPropertyDescriptor(
      HTMLTextAreaElement.prototype,
      "value",
    ).set.call(area, config.instructions);
    area.dispatchEvent(new Event("input", { bubbles: true }));
  }
  if (config.memory === "project_only") {
    let choice = find(
      dialog,
      /^(project.only|yalnızca proje|sadece proje)(\s|$)/i,
    );
    if (!choice) {
      const memory = find(
        dialog,
        /^(memory|bellek|default memory|varsayılan bellek|project settings|proje ayarları)$/i,
      );
      if (memory) {
        memory.dispatchEvent(
          new PointerEvent("pointerdown", {
            bubbles: true,
            button: 0,
            pointerType: "mouse",
            ctrlKey: false,
          }),
        );
        memory.click();
      }
      choice = await wait(() =>
        find(document, /^(project.only|yalnızca proje|sadece proje)(\s|$)/i),
      );
    }
    choice.click();
  }
  if (config.visibility === "public") {
    const choice = find(
      dialog,
      /^(public|anyone with (the )?link|herkese açık|bağlantıya sahip herkes)$/i,
    );
    // Sharing is often a separate post-creation operation. Never silently
    // create a private project when the user requested a shared one.
    if (!choice)
      throw Error(
        "Paylaşım oluşturma ekranında yok; bağlı tarayıcıdaki paylaşım adımını kullanın",
      );
    choice.click();
  }
  const submit = await wait(() =>
    find(dialog, /^(create project|create|proje oluştur|oluştur)$/i),
  );
  submit.click();
  await wait(() => !document.contains(dialog));
  return { submitted: true };
}

async function main(config) {
  const page = await openPage(config, "https://chatgpt.com", "/");
  try {
    await page.cdp.call("Page.bringToFront");
    for (let i = 0; i < 200; i++) {
      if (
        await page.evaluate(
          () =>
            location.origin === "https://chatgpt.com" &&
            document.readyState !== "loading",
        )
      )
        break;
      await new Promise((r) => setTimeout(r, 100));
    }
    const result = await page.evaluate(createInPage, config);
    process.stdout.write(JSON.stringify(result));
  } finally {
    await page.close();
    await openPage(
      { ...config, tab_id: page.id, mode: "close" },
      "https://chatgpt.com",
    );
  }
}

module.exports = { createInPage };
if (require.main === module) {
  const config = JSON.parse(require("node:fs").readFileSync(0, "utf8"));
  main(config).catch((error) => {
    process.stderr.write(error.message);
    process.exitCode = 1;
  });
}
