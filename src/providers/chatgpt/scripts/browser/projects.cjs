"use strict";
const { projectUI, main } = require("../../../scripts/project-ui.cjs");
async function run(c) {
  if (c.project_id && !/^g-p-[a-f0-9]+$/i.test(c.project_id))
    throw Error("Invalid ChatGPT project");
  const ui = await projectUI(
    c,
    "https://chatgpt.com",
    c.project_id ? "/g/" + c.project_id + "/project" : "/",
  );
  const { p, wait, click, fill, has, chooseFile, pause } = ui;
  if (!p) return null;
  try {
    if (!c.project_id) {
      if (
        !(await has(
          "^(New project|Add new project|Yeni proje|Yeni proje ekle)$",
        ))
      ) {
        await wait(
          () =>
            has(
              "^(Open sidebar|Kenar çubuğunu aç|Kenar çubuğunu genişlet|New project|Yeni proje ekle)$",
            ),
          "sidebar",
          25000,
        );
        if (
          await has(
            "^(Open sidebar|Kenar çubuğunu aç|Kenar çubuğunu genişlet)$",
          )
        )
          await click(
            "^(Open sidebar|Kenar çubuğunu aç|Kenar çubuğunu genişlet)$",
          );
      }
      await p.evaluate(() => {
        const e = [...document.querySelectorAll("button,[role=button]")].find(
          (e) =>
            /^(new project|add new project|yeni proje|yeni proje ekle)$/i.test(
              (e.getAttribute("aria-label") || e.textContent || "").trim(),
            ),
        );
        if (!e) throw Error("New project component unavailable");
        e.click();
      });
      await wait(
        () => p.evaluate(() => !!document.querySelector("[role=dialog] input")),
        "new project",
      );
      const memory = [];
      if (
        await has(
          "^(Default memory|Varsayılan bellek|Memory|Bellek)$",
          "[role=dialog]",
        )
      )
        await click(
          "^(Default memory|Varsayılan bellek|Memory|Bellek)$",
          "[role=dialog]",
        );
      await pause(400);
      const available = await p.evaluate(() =>
        [
          ...document.querySelectorAll(
            "button,[role=radio],[role=option],[role=menuitemradio],[role=menuitem]",
          ),
        ]
          .filter((e) => e.getClientRects().length)
          .map((e) =>
            (e.getAttribute("aria-label") || e.textContent || "").trim(),
          ),
      );
      if (available.some((s) => /^(Default|Varsayılan)(\s|$)/i.test(s)))
        memory.push({ id: "default", title: "Varsayılan" });
      if (
        available.some((s) =>
          /^(Project.only|Yalnızca proje|Sadece proje)(\s|$)/i.test(s),
        )
      )
        memory.push({ id: "project_only", title: "Yalnızca proje" });
      const visibility = [];
      if (await has("^(Public|Anyone with the link|Herkese açık)$"))
        visibility.push(
          { id: "private", title: "Özel" },
          { id: "public", title: "Paylaşılan" },
        );
      return { fields: ["name"], memory, visibility };
    }
    await wait(() => has("^(Project actions|Proje işlemleri)$"), "project");
    const settings = async () => {
      await click("^(Project actions|Proje işlemleri)$");
      await click("^(Project settings|Proje ayarları)$");
      await wait(
        () => p.evaluate(() => !!document.querySelector("[role=dialog] input")),
        "settings",
      );
    };
    const sources = async () => {
      await click("^(Sources|Kaynaklar)$");
      await click("^(Add source|Kaynak ekle)$");
      await wait(
        () => has("^(Upload|Yükle|Paste text|Metin yapıştır)$"),
        "source types",
      );
    };
    if (c.mode === "options") {
      await settings();
      const fields = await p.evaluate(() => [
        ...(document.querySelector("[role=dialog] input:not([readonly])")
          ? ["name"]
          : []),
        ...(document.querySelector("[role=dialog] textarea:not([readonly])")
          ? ["instructions"]
          : []),
      ]);
      await click("^(Close dialog|İletişim kutusunu kapat)$", "[role=dialog]");
      try {
        await sources();
        const result = [];
        if (await has("^(Upload|Yükle)$"))
          result.push({
            id: "file",
            title: "Dosya",
            accept: await chooseFile("^(Upload|Yükle)$"),
          });
        if (await has("^(Paste text|Metin yapıştır)$"))
          result.push({ id: "text", title: "Metin" });
        return { fields, sources: result, remove_sources: true };
      } catch (error) {
        return {
          fields,
          sources: [],
          source_error: "Kaynak bileşeni: " + error.message,
        };
      }
    }
    if (c.mode === "update") {
      await settings();
      if (c.name !== undefined)
        await fill("[role=dialog] input[type=text]", c.name);
      if (c.instructions !== undefined)
        await fill("[role=dialog] textarea", c.instructions);
      await click("^(Save|Kaydet)$", "[role=dialog]");
      await wait(
        () =>
          p.evaluate(() => !document.querySelector("[role=dialog] textarea")),
        "settings saved",
      );
      return null;
    }
    if (c.mode === "add_source") {
      await sources();
      if (c.kind === "file") await chooseFile("^(Upload|Yükle)$", c.path);
      else if (c.kind === "text") {
        await click("^(Paste text|Metin yapıştır)$");
        await fill("[role=dialog] input", c.name);
        await fill("[role=dialog] textarea", c.text);
        await click("^(Save|Kaydet)$", "[role=dialog]");
      } else throw Error("Unsupported ChatGPT project source");
      await pause(800);
      return null;
    }
    if (c.mode === "remove_source") {
      await click("^(Sources|Kaynaklar)$");
      const label = await wait(
        () =>
          p.evaluate((name) => {
            const b = [...document.querySelectorAll("button")].find(
              (e) =>
                e.getClientRects().length &&
                (e.getAttribute("aria-label") || "").includes(name) &&
                /actions|işlemler|options|seçenekler/i.test(
                  e.getAttribute("aria-label") || "",
                ),
            );
            return b?.getAttribute("aria-label");
          }, c.resource_name),
        "source row",
      );
      if (!label) throw Error("ChatGPT kaynak menüsü bulunamadı");
      await click("^" + label.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") + "$");
      await click("^(Remove from project|Delete|Projeden kaldır|Sil)$");
      if (await has("^(Delete|Sil|Remove|Kaldır)$", "[role=dialog]"))
        await click("^(Delete|Sil|Remove|Kaldır)$", "[role=dialog]");
      return null;
    }
    throw Error("Unsupported ChatGPT project operation");
  } finally {
    await p.close();
  }
}
module.exports = { run };
if (require.main === module) main(run);
