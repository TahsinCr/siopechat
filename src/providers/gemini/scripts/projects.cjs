"use strict";
const { projectUI, main } = require("../../scripts/project-ui.cjs");
const { nativeBatch } = require("./browser.cjs");
async function run(c) {
  if (c.project_id && !/^notebooks\/[A-Za-z0-9_-]+$/.test(c.project_id))
    throw Error("Invalid Gemini notebook");
  const path = c.project_id
    ? "/notebook/" + c.project_id.split("/")[1]
    : "/notebooks/create";
  const ui = await projectUI(c, "https://gemini.google.com", path);
  const { p, wait, fill, has, chooseFile, pause } = ui;
  const click = (pattern, root = "body") => ui.click(pattern, root, true);
  if (!p) return null;
  try {
    if (!c.project_id) {
      const selector =
        'input[aria-label="Notebook name"],input[aria-label="Not defterinin adı"]';
      await wait(
        () => p.evaluate((s) => !!document.querySelector(s), selector),
        "notebook form",
      );
      if (c.mode === "options") return { fields: ["name"] };
      if (c.mode !== "create") throw Error("Notebook identity required");
      await fill(selector, c.name);
      await click("^(Organize your ideas|Fikirlerinizi organize edin)");
      await click("^(Create notebook|Not defteri oluştur|Create|Oluştur)$");
      return await wait(
        () =>
          p.evaluate(() => {
            const id = location.pathname.match(
              /^\/notebook\/([A-Za-z0-9_-]+)$/,
            )?.[1];
            return id ? "notebooks/" + id : null;
          }),
        "created notebook",
        20000,
      );
    }
    await wait(
      () => has("^(Notebook settings|Not defteri ayarları)$"),
      "notebook",
    );
    if (c.mode !== "options") await p.cdp.call("Page.bringToFront");
    const menu = () => click("^(Notebook settings|Not defteri ayarları)$");
    const settings = async () => {
      await menu();
      await click("^(Notebook settings|Not defteri ayarları)$");
      await wait(
        () =>
          p.evaluate(
            () =>
              !!document.querySelector(
                "[role=dialog] textarea,mat-dialog-container textarea",
              ),
          ),
        "notebook instructions",
      );
    };
    const sourceMenu = async () => {
      await click("^(Add sources?|Kaynak ekle|[0-9]+ Sources?|[0-9]+ Kaynak)$");
      await wait(
        () =>
          has("^(Copied text|Kopyalanan metin|Add website|Web sitesi ekle)$"),
        "source types",
      );
    };
    if (c.mode === "options") {
      await settings();
      const fields = ["name"];
      if (
        await p.evaluate(
          () =>
            !!document.querySelector(
              "textarea:not([disabled]):not([readonly])",
            ),
        )
      )
        fields.push("instructions");
      await click("^(Cancel|İptal)$");
      try {
        await sourceMenu();
        const sources = [];
        if (await has("^(Upload files?|Dosya yükle)"))
          sources.push({
            id: "file",
            title: "Dosya",
            accept: await chooseFile("^(Upload files?|Dosya yükle)"),
          });
        if (await has("^(Copied text|Kopyalanan metin)$"))
          sources.push({ id: "text", title: "Metin" });
        if (await has("^(Add website|Web sitesi ekle)$"))
          sources.push({ id: "url", title: "Web bağlantısı" });
        return { fields, sources, remove_sources: true };
      } catch (error) {
        return {
          fields,
          sources: [],
          source_error: "Kaynak bileşeni: " + error.message,
        };
      }
    }
    if (c.mode === "update") {
      if (c.name !== undefined) {
        await p.evaluate(async () => {
          const wait = async (f) => {
            for (let i = 0; i < 150; i++) {
              const v = f();
              if (v) return v;
              await new Promise((r) => setTimeout(r, 100));
            }
            throw Error("Gemini rename component unavailable");
          };
          const trigger = await wait(() =>
            [...document.querySelectorAll("button")].find(
              (e) =>
                /^(Notebook settings|Not defteri ayarları)$/i.test(
                  e.getAttribute("aria-label") || "",
                ) && e.getClientRects().length,
            ),
          );
          trigger.click();
          const item = await wait(() =>
            document.querySelector("[data-test-id=project-rename-button]"),
          );
          await new Promise((r) => setTimeout(r, 500));
          (item.querySelector(".label") || item).click();
        });
        await fill(
          'input[aria-label="Rename notebook"],input[aria-label="Not defterini yeniden adlandır"]',
          c.name,
        );
        await click("^(Save|Kaydet)$", "mat-dialog-container");
        await wait(
          () => p.evaluate(() => !document.querySelector("edit-title-dialog")),
          "rename saved",
        );
      }
      if (c.instructions !== undefined) {
        await settings();
        await fill(
          "[role=dialog] textarea,mat-dialog-container textarea",
          c.instructions,
        );
        await click("^(Save|Kaydet)$", "mat-dialog-container");
      }
      return null;
    }
    if (c.mode === "add_source") {
      await sourceMenu();
      if (c.kind === "file")
        await chooseFile("^(Upload files?|Dosya yükle)", c.path);
      else if (c.kind === "text") {
        await click("^(Copied text|Kopyalanan metin)$");
        await fill(
          'textarea[aria-label="Pasted text"],textarea[aria-label="Yapıştırılan metin"]',
          c.name + "\n\n" + c.text,
        );
        await click("^(Add text|Metin ekle)$");
      } else if (c.kind === "url") {
        const url = new URL(c.url);
        if (!["https:", "http:"].includes(url.protocol))
          throw Error("Web source requires HTTP(S)");
        await click("^(Add website|Web sitesi ekle)$");
        await fill(
          'textarea[aria-label="Paste links here"],textarea[aria-label="Bağlantıları buraya yapıştırın"]',
          c.url,
        );
        await click("^(Add|Ekle)$", "[role=dialog]");
      } else throw Error("Unsupported Gemini source type");
      await pause(900);
      return null;
    }
    if (c.mode === "remove_source") {
      if (
        !c.resource_id?.startsWith(c.project_id + "/sources/") ||
        !/^notebooks\/[A-Za-z0-9_-]+\/sources\/[A-Za-z0-9_-]+$/.test(
          c.resource_id,
        )
      )
        throw Error("Source belongs to another notebook");
      // Exact source identity from the native removal request; labels are truncated.
      await p.evaluate(nativeBatch, "AptDmf", [[c.resource_id, [1, 3]]], true);
      return null;
    }
    throw Error("Unsupported Gemini notebook operation");
  } finally {
    await p.close();
  }
}
module.exports = { run };
if (require.main === module) main(run);
