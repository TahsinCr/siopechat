#!/usr/bin/env node

// Run Python-verified published Sentinel SDK source in a bounded Node VM.
// Its verification handshake uses a local bridge; the helper makes no requests.

const vm = require("vm");
const { readJsonInput: readStdinJson } = require("../io.cjs");
const maxSdkBytes = 8 * 1024 * 1024;
function writeResult(value) {
  // Sentinel can leave a fallback timeout pending after token() has resolved.
  // This helper is a one-shot process, so exit after its result reaches the caller.
  process.stdout.write(`${JSON.stringify(value)}\n`, (error) => process.exit(error ? 1 : 0));
}

async function main() {
  const input = await readStdinJson();
  const loadedSdk = input?.sdk;
  if (typeof loadedSdk?.implementation !== "string" ||
      typeof loadedSdk?.source !== "string" ||
      Buffer.byteLength(loadedSdk.source, "utf8") > maxSdkBytes) {
    throw new Error("invalid Sentinel SDK resource input");
  }
  const implementation = loadedSdk.implementation;
  const source = loadedSdk.source;
  const suppliedRequirements = input.requirements ?? null;
  let capturedInitial;
  const requestedElements = [];
  const appendedElements = [];
  const listeners = new Map();
  const noop = () => {};
  let sandbox;
  const bridgeWindow = {
    postMessage: (message) => {
      capturedInitial = message?.p;
      if (!message?.requestId || !listeners.has("message")) return;
      const requirements = suppliedRequirements || {
        token: "probe-requirements-token",
        proofofwork: {
          required: true,
          seed: "chatgpt-web-node-probe",
          difficulty: "0fffff",
        },
        turnstile: { required: true, dx: "probe-turnstile-config" },
      };
      setTimeout(() => listeners.get("message")({
        source: bridgeWindow,
        origin: "https://chatgpt.com",
        data: {
          type: "response",
          requestId: message.requestId,
          result: {
            cachedChatReq: requirements,
            cachedProof: message.p,
          },
        },
      }), 0);
    },
  };
  const currentScript = { src: implementation };
  const document = {
    cookie: "",
    currentScript,
    scripts: [currentScript],
    location: { href: implementation, origin: "https://chatgpt.com" },
    documentElement: { getAttribute: () => null },
    body: { appendChild: (element) => { appendedElements.push(element.tagName); } },
    createElement: (tag) => {
      requestedElements.push(tag);
      return {
        tagName: tag.toUpperCase(),
        style: {},
        setAttribute: noop,
        addEventListener: (name, callback) => {
          if (name === "load") setTimeout(callback, 0);
        },
        removeEventListener: noop,
        contentWindow: bridgeWindow,
      };
    },
    addEventListener: noop,
    removeEventListener: noop,
    querySelector: () => null,
  };
  sandbox = {
    console,
    crypto: globalThis.crypto,
    TextEncoder,
    TextDecoder,
    URL,
    URLSearchParams,
    setTimeout,
    clearTimeout,
    setInterval,
    clearInterval,
    performance,
    Response,
    Request,
    Headers,
    AbortController,
    atob,
    btoa,
    screen: { width: 1920, height: 1080, colorDepth: 24, pixelDepth: 24 },
    navigator: {
      userAgent: `Node.js ${process.version}`,
      language: "en-US",
      languages: ["en-US"],
      platform: process.platform,
      hardwareConcurrency: 8,
      deviceMemory: 8,
    },
    document,
    location: { href: "https://chatgpt.com/", origin: "https://chatgpt.com" },
    requestIdleCallback: (callback) => setTimeout(
      () => callback({ timeRemaining: () => 10, didTimeout: false }), 0),
    addEventListener: (name, callback) => listeners.set(name, callback),
    removeEventListener: (name) => listeners.delete(name),
    dispatchEvent: noop,
    fetch: async () => { throw new Error("SDK network access disabled by probe"); },
  };
  sandbox.window = sandbox;
  sandbox.self = sandbox;
  sandbox.top = sandbox;
  sandbox.parent = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(source, sandbox, { filename: "sentinel-sdk.js", timeout: 5000 });

  if (process.argv.includes("--emit-initial")) {
    await sandbox.SentinelSDK.init("siopechat");
    if (typeof capturedInitial !== "string" || !capturedInitial) {
      throw new Error("Sentinel SDK did not generate an initial proof");
    }
    writeResult({ initial: capturedInitial });
    return;
  }

  if (process.argv.includes("--emit-enforcement-json")) {
    await sandbox.SentinelSDK.init("siopechat");
    const token = JSON.parse(await sandbox.SentinelSDK.token("siopechat"));
    if (typeof capturedInitial !== "string" || !capturedInitial) {
      throw new Error("Sentinel SDK did not generate an initial proof");
    }
    if (typeof token?.p !== "string" || !token.p) {
      throw new Error("Sentinel SDK did not generate an enforcement proof");
    }
    const turnstile = typeof token.t === "string" && token.t ? token.t : null;
    writeResult({ initial: capturedInitial, proof: token.p, turnstile });
    return;
  }
  let tokenResult;
  let tokenError;
  try {
    tokenResult = await Promise.race([
      sandbox.SentinelSDK.token("chatgpt-web-node-probe"),
      new Promise((_, reject) => setTimeout(
        () => reject(new Error("token generation timed out")), 3000)),
    ]);
  } catch (error) {
    tokenError = error.message;
  }
  let tokenKeys = [];
  let tokenShape = {};
  if (typeof tokenResult === "string") {
    try {
      const parsedToken = JSON.parse(tokenResult);
      tokenKeys = Object.keys(parsedToken).sort();
      tokenShape = Object.fromEntries(tokenKeys.map((key) => {
        const value = parsedToken[key];
        return [key, {
          type: value === null ? "null" : typeof value,
          length: typeof value === "string" ? value.length : null,
        }];
      }));
    }
    catch (_error) { tokenKeys = []; }
  }
  const result = {
    sdk_url: implementation,
    sdk_sha256: loadedSdk.sha256,
    cache_status: loadedSdk.cache_status,
    public_api: Object.keys(sandbox.SentinelSDK || {}).sort(),
    proof_generated: typeof proof === "string" && proof.startsWith("gAAAAAB"),
    proof_length: typeof proof === "string" ? proof.length : 0,
    requested_elements: [...new Set(requestedElements)],
    appended_elements: [...new Set(appendedElements)],
    message_bridge_installed: listeners.has("message"),
    full_token_generated: typeof tokenResult === "string",
    full_token_keys: tokenKeys,
    full_token_shape: tokenShape,
    full_token_error: tokenError || null,
  };
  writeResult(result);
}

main().catch((error) => {
  process.stderr.write(`sentinel-node-probe: ${error.message}\n`);
  process.exit(1);
});
