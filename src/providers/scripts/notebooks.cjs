"use strict";
const fs = require("node:fs");

function validateRemote(remote) {
  if (
    typeof remote !== "string" ||
    !/^notebooks\/[A-Za-z0-9_-]{6,}$/.test(remote)
  )
    throw Error("invalid Gemini notebook identity");
  return remote;
}

function parseBatch(body, rpcid) {
  const parts = [];
  for (const line of body.split("\n")) {
    if (!line.startsWith('[["wrb.fr"')) continue;
    for (const part of JSON.parse(line)) {
      if (part[0] !== "wrb.fr" || part[1] !== rpcid) continue;
      if (!part[2])
        throw Error("notebook access denied or native contract changed");
      parts.push(JSON.parse(part[2]));
    }
  }
  if (!parts.length) throw Error("notebook response changed");
  return parts;
}

async function nativeRequest(rpcid, payload, mode, cursor) {
  if (location.origin !== "https://gemini.google.com")
    throw Error("unsupported notebook origin");
  const settings = window.WIZ_global_data;
  if (!settings?.SNlM0e) throw Error("Gemini sign-in required");
  const language = document.documentElement.lang || "en";
  const args =
    mode === "chats"
      ? [10, cursor || null, [null, null, 1, payload, 1]]
      : payload === null
        ? [2, [language], 0, null, [2]]
        : [payload, [language], 0];
  const url = new URL("/_/BardChatUi/data/batchexecute", location.origin);
  url.searchParams.set("rpcids", rpcid);
  url.searchParams.set(
    "source-path",
    payload === null
      ? "/notebooks/view"
      : "/notebook/" + payload.slice("notebooks/".length),
  );
  const response = await fetch(url, {
    method: "POST",
    credentials: "same-origin",
    headers: {
      "content-type": "application/x-www-form-urlencoded;charset=UTF-8",
      "x-same-domain": "1",
    },
    body: new URLSearchParams({
      at: settings.SNlM0e,
      "f.req": JSON.stringify([
        [[rpcid, JSON.stringify(args), null, "generic"]],
      ]),
    }),
    signal: AbortSignal.timeout(10000),
  });
  if (!response.ok) throw Error("notebook read failed");
  const body = await response.text();
  if (body.length > 8 * 1024 * 1024)
    throw Error("notebook response exceeds size limit");
  return body;
}

function connect(address) {
  return new Promise((resolve, reject) => {
    const socket = new WebSocket(address),
      pending = new Map();
    let sequence = 0;
    socket.addEventListener("open", () =>
      resolve({
        send(method, params = {}) {
          return new Promise((done, fail) => {
            const id = ++sequence;
            const timer = setTimeout(() => {
              pending.delete(id);
              fail(Error("debugger timeout"));
            }, 5000);
            pending.set(id, { done, fail, timer });
            socket.send(JSON.stringify({ id, method, params }));
          });
        },
        close() {
          for (const entry of pending.values()) {
            clearTimeout(entry.timer);
            entry.fail(Error("closed"));
          }
          pending.clear();
          socket.close();
        },
      }),
    );
    socket.addEventListener("error", reject);
    socket.addEventListener("message", (event) => {
      const data = JSON.parse(event.data),
        entry = pending.get(data.id);
      if (!entry) return;
      clearTimeout(entry.timer);
      pending.delete(data.id);
      data.error
        ? entry.fail(Error("debugger request failed"))
        : entry.done(data.result);
    });
  });
}

async function read(input) {
  const base = new URL(input.cdp_url);
  if (
    base.protocol !== "http:" ||
    !["127.0.0.1", "localhost"].includes(base.hostname)
  )
    throw Error("local debugger required");
  if (input.remote) validateRemote(input.remote);
  if (input.mode && !["get", "list", "chats"].includes(input.mode))
    throw Error("read-only notebook operations required");
  if (
    input.cursor &&
    (typeof input.cursor !== "string" || input.cursor.length > 2048)
  )
    throw Error("invalid cursor");
  const response = await fetch(new URL("/json/list", base), {
    signal: AbortSignal.timeout(3000),
  });
  if (!response.ok) throw Error("debugger unavailable");
  const pages = await response.json();
  const page = pages.find((p) => {
    try {
      return (
        p.type === "page" &&
        new URL(p.url).origin === "https://gemini.google.com"
      );
    } catch {
      return false;
    }
  });
  if (!page) throw Error("open Gemini in the account browser");
  const ws = new URL(page.webSocketDebuggerUrl);
  if (
    ws.protocol !== "ws:" ||
    !["127.0.0.1", "localhost"].includes(ws.hostname) ||
    ws.port !== base.port
  )
    throw Error("non-local debugger");
  const rpcid =
    input.mode === "chats" ? "MaZiqc" : input.remote ? "HcT8bb" : "CNgdBe";
  const connection = await connect(ws.href);
  try {
    const deadline = Date.now() + 7000;
    while (Date.now() < deadline) {
      const ready = await connection.send("Runtime.evaluate", {
        expression: "!!window.WIZ_global_data?.SNlM0e",
        returnByValue: true,
      });
      if (ready.result?.value) break;
      await new Promise((resolve) => setTimeout(resolve, 150));
    }
    const expression = `(${nativeRequest.toString()})(${JSON.stringify(rpcid)},${JSON.stringify(input.remote || null)},${JSON.stringify(input.mode || "list")},${JSON.stringify(input.cursor || null)})`;
    const result = await connection.send("Runtime.evaluate", {
      expression,
      returnByValue: true,
      awaitPromise: true,
    });
    if (result.exceptionDetails || typeof result.result?.value !== "string")
      throw Error(
        "Gemini notebook read unavailable: " +
          (result.exceptionDetails?.exception?.description?.split("\n")[0] ||
            "invalid response"),
      );
    return { parts: parseBatch(result.result.value, rpcid) };
  } finally {
    connection.close();
  }
}

module.exports = { validateRemote, parseBatch, read };
if (require.main === module) {
  read(JSON.parse(fs.readFileSync(0, "utf8")))
    .then((result) => process.stdout.write(JSON.stringify(result)))
    .catch(() => {
      process.stderr.write(
        "Gemini notebook read unavailable; verify the connected account browser",
      );
      process.exitCode = 1;
    });
}
