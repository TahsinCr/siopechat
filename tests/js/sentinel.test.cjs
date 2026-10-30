const test = require("node:test");
const assert = require("node:assert/strict");
const {spawnSync} = require("node:child_process");
const path = require("node:path");

const helper = path.resolve(__dirname, "../../src/providers/chatgpt/scripts/sentinel/sign.cjs");
const sdk = {
  implementation: "https://chatgpt.com/sentinel/test/sdk.js",
  sha256: "test-only",
  cache_status: "test",
  source: `window.SentinelSDK = {
    init: async () => {
      const frame = document.createElement("iframe");
      frame.contentWindow.postMessage({p: "test-initial-context"});
    },
    token: async () => JSON.stringify({p: "gAAAAABtest-enforcement-proof", t: "test-turnstile-token"})
  };`,
};

function run(args, payload) {
  const result = spawnSync(process.execPath, [helper, ...args], {
    input: JSON.stringify(payload),
    encoding: "utf8",
    timeout: 10_000,
  });
  assert.equal(result.status, 0, result.stderr);
  return JSON.parse(result.stdout);
}

test("Sentinel runner executes the supplied SDK context without loading resources itself", () => {
  assert.deepEqual(run(["--emit-initial"], {sdk}), {initial: "test-initial-context"});
});

test("Sentinel runner preserves SDK enforcement and Turnstile outputs", () => {
  assert.deepEqual(
    run(["--emit-enforcement-json"], {
      sdk,
      requirements: {turnstile: {required: true}},
    }),
    {
      initial: "test-initial-context",
      proof: "gAAAAABtest-enforcement-proof",
      turnstile: "test-turnstile-token",
    },
  );
});

test("Sentinel diagnostics report the parsed proof without exposing its value", () => {
  const result = run([], {sdk});

  assert.equal(result.proof_generated, true);
  assert.equal(result.proof_length, "gAAAAABtest-enforcement-proof".length);
  assert.deepEqual(result.full_token_shape.p, {
    type: "string",
    length: "gAAAAABtest-enforcement-proof".length,
  });
  assert.equal(JSON.stringify(result).includes("gAAAAAB"), false);
});
