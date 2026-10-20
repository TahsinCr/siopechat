const test = require("node:test");
const assert = require("node:assert/strict");
const vm = require("node:vm");
const fs = require("node:fs");
const path = require("node:path");
const {createRequire} = require("node:module");
const helper = path.resolve(__dirname, "../../src/providers/chatgpt/scripts/sentinel/sign.cjs");
function functions(fetch) {
  const source = fs.readFileSync(helper, "utf8").split("async function main()")[0];
  const context = {require: createRequire(helper), module: {exports: {}}, process, URL, Buffer, AbortSignal, fetch};
  vm.runInNewContext(source + "\nmodule.exports = {sdkUrl, sdkText};", context);
  return context.module.exports;
}
test("Sentinel source rejects untrusted origins, credentials and ports", () => {
  const {sdkUrl} = functions();
  for (const url of ["http://chatgpt.com/sdk.js", "https://chatgpt.com.evil.test/sdk.js", "https://user@chatgpt.com/sdk.js", "https://chatgpt.com:444/sdk.js"]) {
    assert.throws(() => sdkUrl(url));
  }
  assert.equal(sdkUrl("https://sentinel.openai.com/sdk.js").hostname, "sentinel.openai.com");
});
test("Sentinel fetch bounds source size and rejects redirects", async () => {
  let options;
  const {sdkText} = functions(async (_, value) => {
    options = value;
    return {ok: true, body: (async function* () {yield Buffer.alloc(8 * 1024 * 1024 + 1);})()};
  });
  await assert.rejects(sdkText("https://chatgpt.com/sdk.js"), /too large/);
  assert.equal(options.redirect, "error");
  assert.ok(options.signal instanceof AbortSignal);
});
