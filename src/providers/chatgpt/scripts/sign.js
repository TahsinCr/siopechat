"use strict";

/*
 * Merged Sentinel I/O protocol and isolated QuickJS-NG SDK execution runtime.
 * Validates bounded JSON inputs and runs the verified published Sentinel SDK
 * against a local postMessage bridge without external network access.
 */

const MAX_JSON_INPUT_BYTES = 16 * 1024 * 1024;
const MAX_SDK_BYTES = 8 * 1024 * 1024;

function utf8ByteLength(value) {
  const text = String(value);
  let bytes = 0;
  for (let index = 0; index < text.length; index++) {
    const code = text.charCodeAt(index);
    if (code <= 0x7f) {
      bytes += 1;
    } else if (code <= 0x7ff) {
      bytes += 2;
    } else if (code >= 0xd800 && code <= 0xdbff) {
      if (index + 1 < text.length) {
        const next = text.charCodeAt(index + 1);
        if (next >= 0xdc00 && next <= 0xdfff) {
          bytes += 4;
          index++;
          continue;
        }
      }
      bytes += 3;
    } else {
      bytes += 3;
    }
  }
  return bytes;
}

function readJsonInput(rawInput = globalThis.__siopechatInputJson, maximum = MAX_JSON_INPUT_BYTES) {
  if (typeof rawInput !== "string") {
    throw new Error("invalid JSON input");
  }
  if (utf8ByteLength(rawInput) > maximum) {
    throw new Error("JSON input exceeds size limit");
  }
  try {
    return JSON.parse(rawInput);
  } catch {
    throw new Error("invalid JSON input");
  }
}

const listeners = new Map();
const timers = new Map();
let timerId = 0;
const requestedElements = [];
const appendedElements = [];
let capturedInitial = null;
let suppliedRequirements = null;

const timeOrigin = Date.now();
const performance = {
  timeOrigin,
  now: () => Date.now() - timeOrigin,
  memory: {
    jsHeapSizeLimit: 2172649472,
    totalJSHeapSize: 16777216,
    usedJSHeapSize: 8388608,
  },
};

function setTimeout(callback, delay = 0, ...args) {
  const id = ++timerId;
  timers.set(id, {
    callback,
    args,
    due: Date.now() + Math.max(0, Number(delay) || 0),
    interval: 0,
  });
  return id;
}

function clearTimeout(id) {
  timers.delete(id);
}

function setInterval(callback, delay = 0, ...args) {
  const id = ++timerId;
  const interval = Math.max(1, Number(delay) || 0);
  timers.set(id, { callback, args, due: Date.now() + interval, interval });
  return id;
}

function __siopechatRunDueTimers() {
  const now = Date.now();
  const due = [];
  for (const entry of timers.entries()) {
    if (entry[1].due <= now) due.push(entry);
  }
  due.sort((left, right) => left[1].due - right[1].due || left[0] - right[0]);
  for (const [id, timer] of due) {
    if (!timers.has(id)) continue;
    if (timer.interval) timer.due = now + timer.interval;
    else timers.delete(id);
    try {
      timer.callback(...timer.args);
    } catch {
      // Timer callbacks in Sentinel are fire-and-forget.
    }
  }
  return due.length;
}

function __siopechatNextTimerDelay() {
  let due = Infinity;
  for (const timer of timers.values()) {
    if (timer.due < due) due = timer.due;
  }
  return due === Infinity ? -1 : Math.max(0, due - Date.now());
}

function __siopechatStep() {
  if (globalThis.__siopechatDone === true) return 0;
  if (__siopechatRunDueTimers() > 0) return 1;
  const delay = __siopechatNextTimerDelay();
  return delay < 0 ? -1 : 2 + delay;
}

class TextEncoder {
  encode(value = "") {
    const bytes = [];
    const text = String(value);
    for (let index = 0; index < text.length; index++) {
      let point = text.codePointAt(index);
      if (point > 0xffff) index++;
      else if (point >= 0xd800 && point <= 0xdfff) point = 0xfffd;
      if (point <= 0x7f) {
        bytes.push(point);
      } else if (point <= 0x7ff) {
        bytes.push(0xc0 | (point >> 6), 0x80 | (point & 0x3f));
      } else if (point <= 0xffff) {
        bytes.push(0xe0 | (point >> 12), 0x80 | ((point >> 6) & 0x3f), 0x80 | (point & 0x3f));
      } else {
        bytes.push(
          0xf0 | (point >> 18),
          0x80 | ((point >> 12) & 0x3f),
          0x80 | ((point >> 6) & 0x3f),
          0x80 | (point & 0x3f),
        );
      }
    }
    return new Uint8Array(bytes);
  }
}

class TextDecoder {
  decode(bytes = new Uint8Array()) {
    const view = bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes);
    let output = "";
    for (let index = 0; index < view.length; index++) {
      output += String.fromCharCode(view[index]);
    }
    try {
      return decodeURIComponent(escape(output));
    } catch {
      return output;
    }
  }
}

const base64Alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

function btoa(value) {
  const text = String(value);
  let output = "";
  for (let index = 0; index < text.length; index += 3) {
    const first = text.charCodeAt(index);
    const hasSecond = index + 1 < text.length;
    const hasThird = index + 2 < text.length;
    const second = hasSecond ? text.charCodeAt(index + 1) : 0;
    const third = hasThird ? text.charCodeAt(index + 2) : 0;
    if (first > 255 || second > 255 || third > 255) {
      throw new TypeError("Invalid character");
    }
    output += base64Alphabet[first >> 2];
    output += base64Alphabet[((first & 3) << 4) | (second >> 4)];
    output += hasSecond ? base64Alphabet[((second & 15) << 2) | (third >> 6)] : "=";
    output += hasThird ? base64Alphabet[third & 63] : "=";
  }
  return output;
}

function atob(value) {
  const text = String(value).replace(/[\t\n\f\r ]/g, "");
  if (text.length % 4 === 1) throw new TypeError("Invalid base64");
  let output = "";
  for (let index = 0; index < text.length; index += 4) {
    const first = base64Alphabet.indexOf(text[index]);
    const second = base64Alphabet.indexOf(text[index + 1]);
    const third = text[index + 2] === "=" ? -1 : base64Alphabet.indexOf(text[index + 2]);
    const fourth = text[index + 3] === "=" ? -1 : base64Alphabet.indexOf(text[index + 3]);
    if (
      first < 0 ||
      second < 0 ||
      (third < 0 && text[index + 2] !== "=") ||
      (fourth < 0 && text[index + 3] !== "=")
    ) {
      throw new TypeError("Invalid base64");
    }
    output += String.fromCharCode((first << 2) | (second >> 4));
    if (third >= 0) output += String.fromCharCode(((second & 15) << 4) | (third >> 2));
    if (fourth >= 0) output += String.fromCharCode(((third & 3) << 6) | fourth);
  }
  return output;
}

class URL {
  constructor(value, base) {
    let href = String(value);
    if (!/^[a-z][a-z0-9+.-]*:/i.test(href) && base) {
      const baseUrl = new URL(base);
      if (href.startsWith("//")) href = baseUrl.protocol + href;
      else if (href.startsWith("/")) href = baseUrl.origin + href;
      else if (href.startsWith("?")) href = baseUrl.origin + baseUrl.pathname + href;
      else if (href.startsWith("#")) {
        href = baseUrl.origin + baseUrl.pathname + baseUrl.search + href;
      } else {
        const directory = baseUrl.pathname.slice(0, baseUrl.pathname.lastIndexOf("/") + 1);
        href = baseUrl.origin + directory + href;
      }
    }
    const parts = href.match(/^([a-z][a-z0-9+.-]*:)\/\/([^/?#]+)([^?#]*)(\?[^#]*)?(#.*)?$/i);
    if (!parts) throw new TypeError("Invalid URL");
    this.href = href;
    this.protocol = parts[1];
    this.host = parts[2];
    this.hostname = parts[2].replace(/:\d+$/, "");
    this.port = (parts[2].match(/:(\d+)$/) || [])[1] || "";
    this.pathname = parts[3] || "/";
    this.search = parts[4] || "";
    this.hash = parts[5] || "";
    this.origin = `${this.protocol}//${this.host}`;
    this.username = "";
    this.password = "";
  }

  toString() {
    return this.href;
  }

  toJSON() {
    return this.href;
  }
}

class URLSearchParams {
  constructor(query = "") {
    this.entriesList = [];
    for (const part of String(query).replace(/^\?/, "").split("&")) {
      if (!part) continue;
      const [key, value = ""] = part.split("=");
      this.entriesList.push([
        decodeURIComponent(key.replace(/\+/g, " ")),
        decodeURIComponent(value.replace(/\+/g, " ")),
      ]);
    }
  }

  *entries() {
    yield* this.entriesList;
  }

  *keys() {
    for (const [key] of this.entriesList) yield key;
  }

  *values() {
    for (const [, value] of this.entriesList) yield value;
  }

  [Symbol.iterator]() {
    return this.entries();
  }

  get(key) {
    return (this.entriesList.find(([name]) => name === String(key)) || [])[1] ?? null;
  }
}

let randomPool = typeof globalThis.__siopechatRandomPool === "string"
  ? globalThis.__siopechatRandomPool
  : "";
let randomOffset = 0;

function getRandomValues(view) {
  if (!ArrayBuffer.isView(view)) throw new TypeError("Expected a typed array");
  const neededHex = view.byteLength * 2;
  if (randomOffset + neededHex > randomPool.length) {
    if (typeof globalThis.__siopechatRandomHex === "function") {
      randomPool = globalThis.__siopechatRandomHex(Math.max(view.byteLength, 2048));
      randomOffset = 0;
    } else {
      throw new Error("Secure random pool exhausted");
    }
  }
  const bytes = new Uint8Array(view.buffer, view.byteOffset, view.byteLength);
  for (let index = 0; index < bytes.length; index++) {
    const offset = randomOffset + index * 2;
    bytes[index] = Number.parseInt(randomPool.slice(offset, offset + 2), 16);
  }
  randomOffset += neededHex;
  return view;
}

const crypto = {
  getRandomValues,
  randomUUID() {
    const bytes = getRandomValues(new Uint8Array(16));
    bytes[6] = (bytes[6] & 0x0f) | 0x40;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    const hex = Array.from(bytes, value => value.toString(16).padStart(2, "0")).join("");
    return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
  },
};

class Event {
  constructor(type, options = {}) {
    this.type = String(type);
    this.cancelable = options.cancelable === true;
    this.defaultPrevented = false;
  }

  preventDefault() {
    if (this.cancelable) this.defaultPrevented = true;
  }
}

class AbortSignal {
  constructor() {
    this.aborted = false;
    this.reason = undefined;
    this.onabort = null;
    this.listeners = [];
  }

  static timeout(delay) {
    const signal = new AbortSignal();
    setTimeout(() => signal._abort(new Error("The operation timed out")), delay);
    return signal;
  }

  addEventListener(type, callback, options = {}) {
    if (type === "abort" && typeof callback === "function") {
      this.listeners.push({ callback, once: options?.once === true });
    }
  }

  removeEventListener(type, callback) {
    if (type === "abort") {
      this.listeners = this.listeners.filter(item => item.callback !== callback);
    }
  }

  throwIfAborted() {
    if (this.aborted) throw this.reason || new Error("The operation was aborted");
  }

  _abort(reason) {
    if (this.aborted) return;
    this.aborted = true;
    this.reason = reason || new Error("The operation was aborted");
    const event = new Event("abort");
    for (const item of this.listeners) item.callback.call(this, event);
    this.listeners = this.listeners.filter(item => !item.once);
    if (typeof this.onabort === "function") this.onabort.call(this, event);
  }
}

class AbortController {
  constructor() {
    this.signal = new AbortSignal();
  }

  abort(reason) {
    this.signal._abort(reason);
  }
}

class Headers {
  constructor(init = {}) {
    this._map = new Map();
    if (init && typeof init === "object") {
      for (const [key, value] of Object.entries(init)) {
        this._map.set(String(key).toLowerCase(), String(value));
      }
    }
  }

  get(name) {
    return this._map.get(String(name).toLowerCase()) ?? null;
  }
}

class Request {
  constructor(input, init = {}) {
    this.url = String(input);
    this.method = init.method || "GET";
    this.headers = new Headers(init.headers);
  }
}

class Response {
  constructor(body = "", init = {}) {
    this._body = String(body);
    this.status = init.status ?? 200;
    this.ok = this.status >= 200 && this.status < 300;
    this.headers = new Headers(init.headers);
  }

  async text() {
    return this._body;
  }

  async json() {
    return JSON.parse(this._body);
  }
}

const noop = () => {};

const bridgeWindow = {
  postMessage(message) {
    if (typeof message?.p === "string" && message.p) {
      if (!capturedInitial) capturedInitial = message.p;
      globalThis.__siopechatInitial = capturedInitial;
    }
    if (!message?.requestId || !listeners.has("message")) return;
    const requirements = suppliedRequirements || {
      token: "probe-requirements-token",
      proofofwork: {
        required: true,
        seed: "chatgpt-web-probe",
        difficulty: "0fffff",
      },
      turnstile: { required: true, dx: "probe-turnstile-config" },
    };
    setTimeout(() => {
      const callbacks = listeners.get("message") || [];
      const event = {
        source: bridgeWindow,
        origin: "https://chatgpt.com",
        data: {
          type: "response",
          requestId: message.requestId,
          result: {
            cachedChatReq: requirements,
            cachedProof: capturedInitial || message.p,
          },
        },
      };
      for (const callback of callbacks) callback(event);
    }, 0);
  },
};

const currentScript = { src: "https://chatgpt.com/backend-api/sentinel/sdk.js" };
const location = {
  href: "https://chatgpt.com/",
  origin: "https://chatgpt.com",
  protocol: "https:",
  host: "chatgpt.com",
  hostname: "chatgpt.com",
  port: "",
  pathname: "/",
  search: "",
  hash: "",
};

const document = {
  cookie: "",
  currentScript,
  scripts: [currentScript],
  location: { href: currentScript.src, origin: "https://chatgpt.com" },
  documentElement: { getAttribute: () => null },
  body: {
    appendChild(element) {
      if (element?.tagName) appendedElements.push(element.tagName);
      if (typeof element?.onload === "function") {
        setTimeout(() => element.onload(new Event("load")), 0);
      }
    },
  },
  head: {
    appendChild(element) {
      if (element?.tagName) appendedElements.push(element.tagName);
      if (typeof element?.onload === "function") {
        setTimeout(() => element.onload(new Event("load")), 0);
      }
    },
  },
  createElement(tag) {
    const rawTag = String(tag);
    requestedElements.push(rawTag);
    return {
      tagName: rawTag.toUpperCase(),
      style: {},
      setAttribute: noop,
      getAttribute: () => null,
      addEventListener(name, callback) {
        if (name === "load" && typeof callback === "function") {
          setTimeout(callback, 0);
        }
      },
      removeEventListener: noop,
      contentWindow: bridgeWindow,
    };
  },
  addEventListener: noop,
  removeEventListener: noop,
  querySelector: () => null,
};

let currentUserAgent = "Mozilla/5.0";
let currentPlatform = "Linux x86_64";

const navigatorPrototype = {};
Object.defineProperties(navigatorPrototype, {
  userAgent: { enumerable: true, get: () => currentUserAgent },
  language: { enumerable: true, value: "en-US" },
  languages: { enumerable: true, value: ["en-US"] },
  platform: { enumerable: true, get: () => currentPlatform },
  hardwareConcurrency: { enumerable: true, value: 8 },
  deviceMemory: { enumerable: true, value: 8 },
});
const navigator = Object.create(navigatorPrototype);

const screen = {
  width: 1920,
  height: 1080,
  availWidth: 1920,
  availHeight: 1080,
  colorDepth: 24,
  pixelDepth: 24,
};
const console = { log: noop, warn: noop, error: noop, info: noop, debug: noop };

function addEventListener(name, callback) {
  if (typeof callback !== "function") return;
  const values = listeners.get(name) || [];
  values.push(callback);
  listeners.set(name, values);
}

function removeEventListener(name, callback) {
  if (!listeners.has(name)) return;
  const remaining = (listeners.get(name) || []).filter(value => value !== callback);
  if (remaining.length) listeners.set(name, remaining);
  else listeners.delete(name);
}

function dispatchEvent(event) {
  for (const callback of listeners.get(event?.type) || []) callback(event);
  return true;
}

function requestIdleCallback(callback) {
  const start = performance.now();
  return setTimeout(
    () =>
      callback({
        didTimeout: false,
        timeRemaining: () => Math.max(0, 50 - (performance.now() - start)),
      }),
    0,
  );
}

async function fetch() {
  throw new Error("SDK network access disabled by local Sentinel runtime");
}

function __siopechatDispose() {
  timers.clear();
  listeners.clear();
}

function __siopechatResetRequest(resetRandom = true) {
  timers.clear();
  capturedInitial = null;
  suppliedRequirements = null;
  if (resetRandom) {
    randomPool = typeof globalThis.__siopechatRandomPool === "string"
      ? globalThis.__siopechatRandomPool
      : "";
    randomOffset = 0;
  }
  requestedElements.length = 0;
  appendedElements.length = 0;
  globalThis.__siopechatInitial = null;
  globalThis.__siopechatToken = null;
  globalThis.__siopechatDone = false;
  globalThis.__siopechatError = null;
}

function __siopechatConfigure(config = {}) {
  if (typeof config.userAgent === "string" && config.userAgent) {
    currentUserAgent = config.userAgent;
  }
  if (typeof config.platform === "string" && config.platform) {
    currentPlatform = config.platform;
  }
  if (typeof config.implementation === "string" && config.implementation) {
    currentScript.src = config.implementation;
    document.location.href = config.implementation;
  }
  if (Array.isArray(config.scripts) && config.scripts.length > 0) {
    document.scripts = config.scripts.map(src => ({ src: String(src) }));
    document.currentScript = document.scripts[document.scripts.length - 1];
  } else {
    document.scripts = [currentScript];
    document.currentScript = currentScript;
  }
  if (typeof globalThis.__siopechatRandomPool === "string" && globalThis.__siopechatRandomPool) {
    randomPool = globalThis.__siopechatRandomPool;
    randomOffset = 0;
  }
}

function __siopechatStartInitial() {
  __siopechatResetRequest(false);
  globalThis.__siopechatDone = false;
  globalThis.__siopechatError = null;
  Promise.resolve()
    .then(() => globalThis.SentinelSDK.init("siopechat"))
    .then(
      () => {
        if (typeof capturedInitial !== "string" || !capturedInitial) {
          globalThis.__siopechatError = "Sentinel SDK did not generate an initial proof";
        }
        globalThis.__siopechatInitial = capturedInitial;
        globalThis.__siopechatDone = true;
      },
      error => {
        globalThis.__siopechatError = String(error?.message || error);
        globalThis.__siopechatDone = true;
      },
    );
}

function __siopechatStartToken() {
  globalThis.__siopechatDone = false;
  globalThis.__siopechatError = null;
  globalThis.__siopechatToken = null;
  if (typeof globalThis.__siopechatRequirementsJson === "string") {
    suppliedRequirements = readJsonInput(globalThis.__siopechatRequirementsJson);
  }
  Promise.resolve()
    .then(async () => {
      if (!capturedInitial) {
        await globalThis.SentinelSDK.init("siopechat");
      }
      return globalThis.SentinelSDK.token("siopechat");
    })
    .then(
      token => {
        globalThis.__siopechatInitial = capturedInitial;
        globalThis.__siopechatToken = token;
        globalThis.__siopechatDone = true;
      },
      error => {
        globalThis.__siopechatError = String(error?.message || error);
        globalThis.__siopechatDone = true;
      },
    );
}

function __siopechatStartCommand() {
  globalThis.__siopechatDone = false;
  globalThis.__siopechatError = null;
  globalThis.__siopechatResultJson = null;

  Promise.resolve()
    .then(async () => {
      const args = Array.isArray(globalThis.__siopechatArgs) ? globalThis.__siopechatArgs : [];
      const input = readJsonInput(globalThis.__siopechatInputJson);
      const loadedSdk = input?.sdk;
      if (
        typeof loadedSdk?.implementation !== "string" ||
        !loadedSdk.implementation ||
        typeof loadedSdk?.source !== "string" ||
        utf8ByteLength(loadedSdk.source) > MAX_SDK_BYTES
      ) {
        throw new Error("invalid Sentinel SDK resource input");
      }
      suppliedRequirements = input.requirements ?? null;
      capturedInitial = null;
      requestedElements.length = 0;
      appendedElements.length = 0;
      __siopechatConfigure({
        implementation: loadedSdk.implementation,
        scripts: loadedSdk.scripts,
        userAgent: input.userAgent,
        platform: input.platform,
      });
      (0, eval)(loadedSdk.source);

      if (args.includes("--emit-initial")) {
        await globalThis.SentinelSDK.init("siopechat");
        if (typeof capturedInitial !== "string" || !capturedInitial) {
          throw new Error("Sentinel SDK did not generate an initial proof");
        }
        return { initial: capturedInitial };
      }

      if (args.includes("--emit-enforcement-json")) {
        await globalThis.SentinelSDK.init("siopechat");
        const token = JSON.parse(await globalThis.SentinelSDK.token("siopechat"));
        if (typeof capturedInitial !== "string" || !capturedInitial) {
          throw new Error("Sentinel SDK did not generate an initial proof");
        }
        if (typeof token?.p !== "string" || !token.p) {
          throw new Error("Sentinel SDK did not generate an enforcement proof");
        }
        const turnstile = typeof token.t === "string" && token.t ? token.t : null;
        return { initial: capturedInitial, proof: token.p, turnstile };
      }

      let tokenResult;
      let tokenError;
      let proofGenerated = false;
      let proofLength = 0;
      try {
        tokenResult = await Promise.race([
          globalThis.SentinelSDK.token("chatgpt-web-probe"),
          new Promise((_, reject) =>
            setTimeout(() => reject(new Error("token generation timed out")), 3000),
          ),
        ]);
      } catch (error) {
        tokenError = error?.message || String(error);
      }

      let tokenKeys = [];
      let tokenShape = {};
      if (typeof tokenResult === "string") {
        try {
          const parsedToken = JSON.parse(tokenResult);
          tokenKeys = Object.keys(parsedToken).sort();
          const proof = parsedToken?.p;
          proofGenerated = typeof proof === "string" && proof.startsWith("gAAAAAB");
          proofLength = typeof proof === "string" ? proof.length : 0;
          tokenShape = Object.fromEntries(
            tokenKeys.map(key => {
              const value = parsedToken[key];
              return [
                key,
                {
                  type: value === null ? "null" : typeof value,
                  length: typeof value === "string" ? value.length : null,
                },
              ];
            }),
          );
        } catch {
          tokenKeys = [];
        }
      }

      return {
        sdk_url: loadedSdk.implementation,
        sdk_sha256: loadedSdk.sha256,
        cache_status: loadedSdk.cache_status,
        public_api: Object.keys(globalThis.SentinelSDK || {}).sort(),
        proof_generated: proofGenerated,
        proof_length: proofLength,
        requested_elements: [...new Set(requestedElements)],
        appended_elements: [...new Set(appendedElements)],
        message_bridge_installed: listeners.has("message"),
        full_token_generated: typeof tokenResult === "string",
        full_token_keys: tokenKeys,
        full_token_shape: tokenShape,
        full_token_error: tokenError || null,
      };
    })
    .then(
      result => {
        globalThis.__siopechatResultJson = JSON.stringify(result);
        globalThis.__siopechatDone = true;
      },
      error => {
        globalThis.__siopechatError = String(error?.message || error);
        globalThis.__siopechatDone = true;
      },
    );
}

Object.assign(globalThis, {
  window: globalThis,
  self: globalThis,
  top: globalThis,
  parent: globalThis,
  crypto,
  performance,
  document,
  navigator,
  location,
  screen,
  console,
  URL,
  URLSearchParams,
  TextEncoder,
  TextDecoder,
  AbortController,
  AbortSignal,
  Event,
  Headers,
  Request,
  Response,
  setTimeout,
  clearTimeout,
  setInterval,
  clearInterval: clearTimeout,
  requestIdleCallback,
  fetch,
  atob,
  btoa,
  addEventListener,
  removeEventListener,
  dispatchEvent,
  readJsonInput,
  __siopechatConfigure,
  __siopechatStartInitial,
  __siopechatStartToken,
  __siopechatStartCommand,
  __siopechatRunDueTimers,
  __siopechatNextTimerDelay,
  __siopechatStep,
  __siopechatDispose,
  __siopechatResetRequest,
});

if (typeof globalThis.__siopechatConfigJson === "string") {
  __siopechatConfigure(readJsonInput(globalThis.__siopechatConfigJson));
}

