const { test } = require("node:test");
const assert = require("node:assert/strict");
const {
  validate,
  findAction,
} = require("../../src/providers/scripts/manage.cjs");
test("project deletion rejects foreign origins, credentials and malformed remote identities", () => {
  for (const config of [
    {
      cdp_url: "http://example.com:9222",
      provider: "chatgpt",
      remote: "g-p-fixture",
    },
    {
      cdp_url: "http://user:secret@127.0.0.1:9222",
      provider: "chatgpt",
      remote: "g-p-fixture",
    },
    {
      cdp_url: "http://127.0.0.1:9222",
      provider: "gemini",
      remote: "../../private",
    },
  ])
    assert.throws(() => validate(config));
  assert.equal(
    validate({
      cdp_url: "http://127.0.0.1:9222",
      provider: "gemini",
      remote: "notebooks/abcdef",
    }).port,
    "9222",
  );
});
function button(text, visible = true) {
  return {
    innerText: text,
    disabled: false,
    getAttribute: () => null,
    getClientRects: () => (visible ? [{}] : []),
    checkVisibility: () => visible,
    scrollIntoView: () => {},
    getBoundingClientRect: () => ({ left: 1, top: 2, width: 20, height: 10 }),
  };
}
test("delete confirmation is scoped to the last visible dialog, never an unrelated delete button", () => {
  const unrelated = button("Sil"),
    confirm = button("Sil");
  const dialog = { ...button("Dialog"), querySelectorAll: () => [confirm] };
  global.document = {
    querySelectorAll: (s) =>
      s.includes("role=dialog") ? [dialog] : [unrelated],
  };
  assert.deepEqual(findAction({ provider: "gemini" }, "confirm"), {
    x: 11,
    y: 7,
    settings: false,
  });
  delete global.document;
});
test("delete_final does not mistake the settings menu for the destructive action", () => {
  global.document = {
    querySelectorAll: () => [button("Proje ayarları")],
    querySelector: () => null,
  };
  assert.equal(findAction({ provider: "chatgpt" }, "delete_final"), null);
  delete global.document;
});

test("moving or obscured overlay controls cannot be clicked using stale coordinates", () => {
  const b = button("Sil");
  b.contains = () => false;
  const d = { ...button("Dialog"), querySelectorAll: () => [b] };
  global.document = {
    querySelectorAll: (s) => (s.includes("role=dialog") ? [d] : []),
    elementFromPoint: () => ({}),
  };
  assert.equal(findAction({ provider: "chatgpt" }, "confirm"), null);
  delete global.document;
});

test("Gemini's delete-everywhere confirmation uses the scoped dialog", () => {
  const b = button("Her yerden sil");
  const d = { ...button("Dialog"), querySelectorAll: () => [b] };
  global.document = {
    querySelectorAll: (s) => (s.includes("role=dialog") ? [d] : []),
  };
  assert.ok(findAction({ provider: "gemini" }, "confirm"));
  delete global.document;
});
test("Gemini custom menu activates its label and confirms delete-everywhere exactly once", async () => {
  const {
    deleteGeminiInPage,
  } = require("../../src/providers/scripts/manage.cjs");
  let state = 0;
  const calls = [];
  const confirm = {
    ...button("Her yerden sil"),
    click: () => {
      calls.push("confirm");
      state = 3;
    },
  };
  const dialog = { ...button("dialog"), querySelectorAll: () => [confirm] };
  const trigger = {
    ...button(""),
    getAttribute: () => "Not defteri ayarları",
    click: () => {
      calls.push("menu");
      state = 1;
    },
  };
  const item = {
    querySelector: () => ({
      click: () => {
        calls.push("label");
        state = 2;
      },
    }),
  };
  global.document = {
    querySelectorAll: (s) =>
      s === "button" ? [trigger] : state === 2 ? [dialog] : [],
    querySelector: () => (state === 1 ? item : null),
    contains: () => state !== 3,
  };
  try {
    await deleteGeminiInPage();
    assert.deepEqual(calls, ["menu", "label", "confirm"]);
  } finally {
    delete global.document;
  }
});

test("ChatGPT's chat-and-work delete label resolves to the exact confirmed control",()=>{
 const confirm=button("Sohbet ve Çalışma'dan sil");
 let focused=false;confirm.focus=()=>{focused=true;};
 const dialog={...button('Dialog'),querySelectorAll:()=>[button('İptal'),confirm]};
 global.document={querySelectorAll:s=>s.includes('role=dialog')?[dialog]:[]};
 try{assert.deepEqual(findAction({provider:'chatgpt'},'confirm'),{x:11,y:7,settings:false});assert.equal(focused,true);}finally{delete global.document;}
});
