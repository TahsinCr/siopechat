"use strict";
async function connect(address, base) {
  const url = new URL(address);
  if (
    !["ws:", "wss:"].includes(url.protocol) ||
    url.hostname !== base.hostname ||
    url.port !== base.port
  )
    throw Error("Non-local debugger socket rejected");
  const ws = new WebSocket(address),
    pending = new Map();
  const listeners = new Map();
  let seq = 0;
  await new Promise((resolve, reject) => {
    ws.addEventListener("open", resolve, { once: true });
    ws.addEventListener("error", reject, { once: true });
  });
  ws.addEventListener("message", (event) => {
    const m = JSON.parse(event.data),
      p = pending.get(m.id);
    if (m.method && listeners.has(m.method)) {
      for (const callback of listeners.get(m.method)) callback(m.params || {});
    }
    if (p) {
      pending.delete(m.id);
      clearTimeout(p.timer);
      m.error ? p.reject(Error(m.error.message)) : p.resolve(m.result);
    }
  });
  return {
    on(method, callback) {
      if (!listeners.has(method)) listeners.set(method, new Set());
      listeners.get(method).add(callback);
    },
    async call(method, params = {}, timeoutMs = 10000) {
      return new Promise((resolve, reject) => {
        const id = ++seq;
        const timer = setTimeout(() => {
          pending.delete(id);
          reject(Error("Debugger operation timed out"));
        }, timeoutMs);
        pending.set(id, { resolve, reject, timer });
        ws.send(JSON.stringify({ id, method, params }));
      });
    },
    close() {
      for (const p of pending.values()) {
        clearTimeout(p.timer);
        p.reject(Error("Debugger closed"));
      }
      pending.clear();
      ws.close();
    },
  };
}

module.exports = { connect };
