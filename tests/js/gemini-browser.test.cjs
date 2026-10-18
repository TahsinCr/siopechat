const { test } = require("node:test");
const assert = require("node:assert/strict");
test("native deletion requires an acknowledged signed-in same-origin RPC", async () => {
  const { deleteChatInPage } = require("../../src/providers/gemini/scripts/browser.cjs");
  const previous = { location: global.location, window: global.window, fetch: global.fetch };
  global.location = { origin: "https://gemini.google.com" };
  global.window = { WIZ_global_data: { SNlM0e: "fixture-token" } };
  let calls = 0;
  global.fetch = async (url, options) => {
    calls++;
    assert.equal(url.origin, "https://gemini.google.com");
    assert.equal(options.credentials, "same-origin");
    assert.equal(options.body.get("at"), "fixture-token");
    assert.equal(JSON.parse(JSON.parse(options.body.get("f.req"))[0][0][1])[0], "c_abc");
    return { ok: true, text: async () => '[["wrb.fr","GzXR5e","null"]]' };
  };
  try {
    assert.equal(await deleteChatInPage("c_abc"), true);
    assert.equal(calls, 1);
    global.fetch = async () => ({ ok: true, text: async () => '[["wrb.fr","GzXR5e",null,null,null,[3]]]' });
    await assert.rejects(deleteChatInPage("c_abc"), /rejected/);
    await assert.rejects(deleteChatInPage("../../private"), /Invalid/);
  } finally {
    Object.assign(global, previous);
  }
});
const {
  validate,
  canonicalModel,
} = require("../../src/providers/gemini/scripts/browser.cjs");
test("native Gemini browser transport rejects remote debuggers and foreign notebook identities", () => {
  assert.throws(() =>
    validate({ mode: "send", cdp_url: "http://example.com:9231" }),
  );
  assert.throws(() =>
    validate({
      mode: "send",
      cdp_url: "http://127.0.0.1:9231",
      project_id: "https://other/notebook/abcdef",
    }),
  );
  assert.throws(() =>
    validate({ mode: "send", cdp_url: "http://user:secret@127.0.0.1:9231" }),
  );
  assert.throws(() =>
    validate({
      mode: "send",
      cdp_url: "http://127.0.0.1:9231",
      conversation_id: "../escape",
    }),
  );
  assert.equal(
    validate({
      mode: "send",
      cdp_url: "http://127.0.0.1:9231",
      project_id: "notebooks/abcdef",
    }).port,
    "9231",
  );
});
test("native model aliases are stable while displayed versions remain dynamic", () => {
  assert.equal(canonicalModel("3.8 Flash"), "gemini-flash");
  assert.equal(canonicalModel("Flash\nOrta"), "gemini-flash");
  assert.equal(canonicalModel("9.2 Flash-Lite"), "gemini-flash-lite");
  assert.equal(canonicalModel("12 New Model"), "gemini-new-model");
});
const {
  renderMarkdown,
} = require("../../src/providers/scripts/markdown.cjs");
function text(value) {
  return { nodeType: 3, textContent: value };
}
function element(tag, children = [], attrs = {}) {
  return {
    nodeType: 1,
    tagName: tag,
    childNodes: children,
    getAttribute: (key) => attrs[key] ?? null,
    querySelector: () => null,
    textContent: children.map((x) => x.textContent || "").join(""),
  };
}
test("native responses preserve Markdown, source links and code instead of flattening text", () => {
  const root = element("DIV", [
    element("H2", [text("Title")]),
    element("P", [
      element("STRONG", [text("Bold")]),
      text(" and "),
      element("A", [text("Source")], { href: "https://example.com/source" }),
    ]),
    element("PRE", [text("print(1)")]),
    element("UL", [element("LI", [text("Item")])]),
  ]);
  const result = renderMarkdown(root);
  assert.match(result, /## Title/);
  assert.match(result, /\*\*Bold\*\*/);
  assert.match(result, /\[Source\]\(https:\/\/example.com\/source\)/);
  assert.ok(result.includes("```\nprint(1)\n```"));
  assert.ok(result.includes("- Item"));
  assert.equal(
    renderMarkdown(
      element("A", [text("safe")], { href: "javascript:alert(1)" }),
    ),
    "safe",
  );
  assert.equal(renderMarkdown(element("BUTTON", [text("Copy")])), "");
});

test('void source acknowledgements require explicit opt-in and still reject RPC errors',async()=>{
  const {nativeBatch}=require('../../src/providers/gemini/scripts/browser.cjs');
  const previous={location:global.location,window:global.window,fetch:global.fetch};
  global.location={origin:'https://gemini.google.com'};
  global.window={WIZ_global_data:{SNlM0e:'fixture'}};
  global.fetch=async()=>({ok:true,text:async()=>'[["wrb.fr","AptDmf",null]]'});
  try {
    await assert.rejects(nativeBatch('AptDmf',[['owned']]),/not confirmed/);
    assert.deepEqual(await nativeBatch('AptDmf',[['owned']],true),[null]);
    global.fetch=async()=>({ok:true,text:async()=>'[["wrb.fr","AptDmf",null,null,null,[3]]]'});
    await assert.rejects(nativeBatch('AptDmf',[['owned']],true),/rejected/);
  }finally{Object.assign(global,previous);}
});

test('page readiness tolerates a navigation context change but never retries other errors',async()=>{
 const{waitFor}=require('../../src/providers/gemini/scripts/browser.cjs');let calls=0;
 assert.equal(await waitFor(()=>{if(!calls++)throw Error('Cannot find default execution context');return true;},1),true);
 assert.equal(calls,2);calls=0;
 await assert.rejects(waitFor(()=>{calls++;throw Error('write outcome unknown');},1),/write outcome unknown/);
 assert.equal(calls,1);
});
