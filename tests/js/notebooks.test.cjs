const { test } = require("node:test");
const assert = require("node:assert/strict");
const {
  validateRemote,
  parseBatch,
  read,
} = require("../../src/providers/scripts/notebooks.cjs");

test("native notebook identities exclude URL injection and other products", () => {
  assert.equal(validateRemote("notebooks/abc-123"), "notebooks/abc-123");
  for (const value of [
    "https://notebooklm.google.com/notebook/abc",
    "javascript:alert(1)",
    "c_chat",
    "notebooks/../../secret",
  ])
    assert.throws(() => validateRemote(value));
});
test("native batch decoder rejects denied or changed responses", () => {
  const body = JSON.stringify([
    ["wrb.fr", "CNgdBe", JSON.stringify([null, null, []])],
  ]);
  assert.deepEqual(parseBatch(body, "CNgdBe"), [[null, null, []]]);
  assert.throws(() => parseBatch("[]", "CNgdBe"));
  assert.throws(() =>
    parseBatch(
      JSON.stringify([["wrb.fr", "CNgdBe", null, null, null, [7]]]),
      "CNgdBe",
    ),
  );
});
test("notebook bridge rejects non-local debugging connections before network access", async () => {
  await assert.rejects(read({ cdp_url: "https://remote.example" }));
});
