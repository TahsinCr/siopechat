#!/usr/bin/env node

// Capture an authenticated ChatGPT account request through Chrome DevTools Protocol.
// The result contains credentials and must only be consumed over a private pipe.

const { findPage } = require("./cdp.cjs");
const { URL } = require("url");

const cdpBase = process.env.CHATGPT_WEB_CDP_URL || "http://127.0.0.1:9222";
const timeoutMs = Number(process.env.CHATGPT_WEB_AUTH_TIMEOUT_MS || "15000");

function fail(message) {
  process.stderr.write(`siopechat ChatGPT auth: ${message}\n`);
  process.exit(1);
}


async function main() {
  const page = await findPage(cdpBase);

  const socket = new WebSocket(page.webSocketDebuggerUrl);
  const requests = new Map();
  const extraHeaders = new Map();
  const responses = new Map();
  let sequence = 0;
  let finished = false;

  const timer = setTimeout(() => {
    if (!finished) {
      socket.close();
      fail("timed out waiting for an authenticated ChatGPT account request");
    }
  }, timeoutMs);

  function send(method, params = {}) {
    socket.send(JSON.stringify({ id: ++sequence, method, params }));
  }

  function isTarget(request) {
    if (request.method !== "GET") {
      return false;
    }

    try {
      const url = new URL(request.url);
      return url.hostname === "chatgpt.com" &&
        url.pathname === "/backend-api/me";
    } catch {
      return false;
    }
  }

  function finish(requestId) {
    if (finished || !requests.has(requestId) || !extraHeaders.has(requestId) ||
        !responses.has(requestId)) {
      return;
    }
    if (responses.get(requestId) < 200 || responses.get(requestId) >= 300) {
      requests.delete(requestId);
      extraHeaders.delete(requestId);
      responses.delete(requestId);
      return;
    }
    finished = true;
    clearTimeout(timer);
    const request = requests.get(requestId);
    const result = {
      url: request.url,
      method: request.method,
      headers: { ...request.headers, ...extraHeaders.get(requestId) },
    };
    process.stdout.write(JSON.stringify(result));
    socket.close();
  }

  socket.addEventListener("open", () => {
    send("Network.enable");
    setTimeout(() => send("Page.navigate", { url: "https://chatgpt.com/" }), 200);
  });
  socket.addEventListener("message", (event) => {
    const message = JSON.parse(event.data);
    if (message.method === "Network.requestWillBeSent") {
      const { requestId, request } = message.params;
      if (isTarget(request)) {
        requests.set(requestId, request);
        finish(requestId);
      }
    } else if (message.method === "Network.requestWillBeSentExtraInfo") {
      const { requestId, headers } = message.params;
      extraHeaders.set(requestId, headers);
      finish(requestId);
    } else if (message.method === "Network.responseReceived") {
      const { requestId, response } = message.params;
      if (requests.has(requestId)) {
        responses.set(requestId, response.status);
        finish(requestId);
      }
    }
  });
  socket.addEventListener("error", () => {
    if (!finished) fail("lost the Chrome debugger connection");
  });
}

main().catch((error) => fail(error.message));
