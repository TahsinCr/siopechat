"use strict";

const http = require("http");
const https = require("https");

function getJson(address, timeoutMs = 5000) {
  return new Promise((resolve, reject) => {
    const url = new URL(address);
    const transport = url.protocol === "https:" ? https : http;
    const request = transport.get(url, (response) => {
      if (response.statusCode < 200 || response.statusCode >= 300) {
        response.resume();
        reject(new Error(`Chrome debugger HTTP ${response.statusCode}`));
        return;
      }
      let body = "";
      let size = 0;
      response.setEncoding("utf8");
      response.on("data", (chunk) => {
        size += Buffer.byteLength(chunk);
        if (size > 2 * 1024 * 1024) {
          response.destroy(new Error("Chrome debugger metadata exceeds size limit"));
          return;
        }
        body += chunk;
      });
      response.on("error", reject);
      response.on("end", () => {
        try { resolve(JSON.parse(body)); }
        catch { reject(new Error("invalid Chrome debugger metadata")); }
      });
    });
    request.setTimeout(timeoutMs, () => request.destroy(new Error("Chrome debugger timed out")));
    request.on("error", reject);
  });
}

function selectPage(pages) {
  if (!Array.isArray(pages)) throw new Error("Chrome debugger returned an invalid page list");
  const page = pages.find((item) => {
    if (item.type !== "page" || typeof item.webSocketDebuggerUrl !== "string") return false;
    try {
      const url = new URL(item.url);
      return url.protocol === "https:" && url.hostname === "chatgpt.com";
    } catch { return false; }
  });
  if (!page) throw new Error("no ChatGPT tab found in the debugging browser");
  return page;
}

async function findPage(base) {
  return selectPage(await getJson(`${base.replace(/\/$/, "")}/json/list`));
}

module.exports = { getJson, selectPage, findPage };
