"use strict";
const { findPage } = require("./cdp.cjs");

// Use the website's own creation form. Do not guess private write endpoints.
async function createInPage(config) {
  const pause = () => new Promise(resolve => setTimeout(resolve, 100));
  const label = element => (element.getAttribute("aria-label") || element.textContent || "").trim();
  const buttons = root => [...root.querySelectorAll('button,[role="button"],[role="radio"],[role="option"]')];
  const find = (root, pattern) => buttons(root).find(element => !element.disabled && pattern.test(label(element)));
  const wait = async callback => {
    for (let i = 0; i < 50; i++) { const value = callback(); if (value) return value; await pause(); }
    throw Error("Proje oluşturma ekranı bulunamadı; bağlı tarayıcıyı kontrol edin");
  };
  let start = find(document, /^(new project|yeni proje)$/i);
  if (!start) {
    const sidebar = find(document, /^(open sidebar|kenar çubuğunu aç|kenar çubuğunu genişlet)$/i);
    if (sidebar) sidebar.click();
    start = await wait(() => find(document, /^(new project|yeni proje)$/i));
  }
  start.click();
  const dialog = await wait(() => [...document.querySelectorAll('[role="dialog"]')].find(node => node.querySelector('input')));
  const input = dialog.querySelector('input[type="text"],input:not([type])');
  if (!input) throw Error("Proje adı alanı değişmiş");
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set;
  setter.call(input, config.name);
  input.dispatchEvent(new Event("input", {bubbles:true}));
  input.dispatchEvent(new Event("change", {bubbles:true}));
  if (config.instructions) {
    const area = dialog.querySelector('textarea');
    if (!area) throw Error("Talimat alanı bu oluşturma ekranında desteklenmiyor");
    Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value").set.call(area, config.instructions);
    area.dispatchEvent(new Event("input", {bubbles:true}));
  }
  if (config.memory === "project_only") {
    let choice = find(dialog, /^(project.only|yalnızca proje|sadece proje)$/i);
    if (!choice) {
      const memory = find(dialog, /^(memory|bellek|project settings|proje ayarları)$/i);
      if (memory) memory.click();
      choice = await wait(() => find(document, /^(project.only|yalnızca proje|sadece proje)$/i));
    }
    choice.click();
  }
  if (config.visibility === "public") {
    const choice = find(dialog, /^(public|anyone with (the )?link|herkese açık|bağlantıya sahip herkes)$/i);
    // Sharing is often a separate post-creation operation. Never silently
    // create a private project when the user requested a shared one.
    if (!choice) throw Error("Paylaşım oluşturma ekranında yok; bağlı tarayıcıdaki paylaşım adımını kullanın");
    choice.click();
  }
  const submit = await wait(() => find(dialog, /^(create project|create|proje oluştur|oluştur)$/i));
  submit.click();
  await wait(() => !document.contains(dialog));
  return {submitted:true};
}

async function main(config) {
  const base = new URL(config.cdp_url);
  if (base.protocol !== "http:" || !["127.0.0.1", "localhost"].includes(base.hostname)) throw Error("local debugger required");
  const page = await findPage(config.cdp_url);
  const socket = new WebSocket(page.webSocketDebuggerUrl);
  const result = await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(Error("Proje işlemi zaman aşımına uğradı; tekrar oluşturmadan tarayıcıyı kontrol edin")), 22000);
    socket.addEventListener("error", () => reject(Error("Bağlı tarayıcı bağlantısı kesildi")));
    socket.addEventListener("open", () => socket.send(JSON.stringify({id:1,method:"Runtime.evaluate",params:{expression:`(${createInPage.toString()})(${JSON.stringify(config)})`,awaitPromise:true,returnByValue:true}})));
    socket.addEventListener("message", event => {
      const message = JSON.parse(event.data);
      if (message.id !== 1) return;
      clearTimeout(timer);
      if (message.error || message.result.exceptionDetails) reject(Error(message.result?.exceptionDetails?.exception?.description?.split("\n")[0] || "Proje oluşturma bileşeni değişmiş"));
      else resolve(message.result.result.value);
    });
  }).finally(() => socket.close());
  process.stdout.write(JSON.stringify(result));
}

module.exports = { createInPage };
if (require.main === module) {
  const config = JSON.parse(require("node:fs").readFileSync(0,"utf8"));
  main(config).catch(error => { process.stderr.write(error.message); process.exitCode = 1; });
}
