"use strict";
const { projectUI, main } = require("../../scripts/project-ui.cjs");
async function run(c) {
  if (c.project_id && !/^[a-f0-9-]{36}$/i.test(c.project_id))
    throw Error("Invalid Claude project");
  const ui = await projectUI(
    c,
    "https://claude.ai",
    c.project_id ? "/project/" + c.project_id : "/projects",
  );
  const { p, wait, click, fill, has, chooseFile, pause } = ui;
  if (!p) return null;
  try {
    if (!c.project_id) return { fields: ["name"] };
    await wait(
      () => has("^(Edit instructions|Talimatları düzenle)$"),
      "project",
    );
    const sourceMenu = async () => {
      await click("^(Add files|Dosya ekle)$");
      await wait(
        () =>
          has(
            "(Upload from device|Cihazdan yükle|Add text content|Metin içeriği ekle)$",
          ),
        "source types",
      );
    };
    if (c.mode === "options") {
      const fields = [];
      if (await has("(Rename|yeniden adlandır)")) fields.push("name");
      if (await has("^(Edit instructions|Talimatları düzenle)$"))
        fields.push("instructions");
      try {
        await sourceMenu();
        const sources = [];
        if (await has("(Upload from device|Cihazdan yükle)$")) {
          const accept = await chooseFile(
            "(Upload from device|Cihazdan yükle)$",
          );
          sources.push({ id: "file", title: "Dosya", accept });
        }
        if (!(await has("(Add text content|Metin içeriği ekle)$")))
          await sourceMenu();
        if (await has("(Add text content|Metin içeriği ekle)$"))
          sources.push({ id: "text", title: "Metin" });
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
        await click("(Rename|yeniden adlandır)");
        await fill(
          'input[aria-label="Project name"],input[aria-label="Proje adı"]',
          c.name,
        );
      }
      if (c.instructions !== undefined) {
        await click("^(Edit instructions|Talimatları düzenle)$");
        await fill(
          '[data-testid="custom-instructions-textarea"]',
          c.instructions,
        );
        await click("^(Save instructions|Talimatları kaydet)$");
      }
      return null;
    }
    if (c.mode === "add_source") {
      await sourceMenu();
      if (c.kind === "file")
        await chooseFile("(Upload from device|Cihazdan yükle)$", c.path);
      else if (c.kind === "text") {
        await click("(Add text content|Metin içeriği ekle)$");
        await fill('[role="dialog"] input', c.name);
        await fill('[role="dialog"] textarea', c.text);
        await click("^(Add content|İçerik ekle)$", "[role=dialog]");
      } else throw Error("Unsupported Claude project source");
      await pause(800);
      return null;
    }
    if (c.mode === "remove_source") {
      const heading = await wait(
        () =>
          p.evaluate((name) => {
            const h = [
              ...document.querySelectorAll('[data-testid="file-thumbnail"] h3'),
            ].filter((e) => e.textContent.trim() === name);
            if (h.length !== 1) return null;
            return h[0].id;
          }, c.resource_name),
        "source row",
      );
      if (!/^[A-Za-z0-9_-]+$/.test(heading))
        throw Error("Invalid source control");
      await p.evaluate((heading) => {
        const row = [
          ...document.querySelectorAll("[data-testid=file-thumbnail]"),
        ].find((e) => e.querySelector("h3")?.id === heading);
        const button = [...(row?.querySelectorAll("button") || [])].find((e) =>
          /^(Remove|Delete|Kaldır|Sil)$/i.test(
            e.getAttribute("aria-label") || "",
          ),
        );
        if (!button || button.disabled)
          throw Error("Source removal control unavailable");
        button.click();
      }, heading);
      await pause(350);
      if (await has("^(Delete|Sil|Remove|Kaldır)$", "[role=dialog]"))
        await click("^(Delete|Sil|Remove|Kaldır)$", "[role=dialog]");
      return null;
    }
    throw Error("Unsupported Claude project operation");
  } finally {
    await p.close();
  }
}
module.exports = { run };
if (require.main === module) main(run);
