const { test } = require("node:test");
const assert = require("node:assert/strict");
const {
  validate,
  modelOptions,
  apiRequest,
} = require("../../src/providers/claude/scripts/browser.cjs");
const id = "12345678-abcd-1234-abcd-123456789abc";
test("Claude validates operation, remote identity and model before touching the browser", () => {
  validate({
    mode: "send",
    prompt: "Hello",
    conversation_id: id,
    model: "claude-future",
  });
  for (const c of [
    { mode: "history", conversation_id: "../../other" },
    { mode: "delete_chat" },
    { mode: "delete_project", project_id: "https://evil.test" },
    { mode: "send", prompt: "" },
    { mode: "send", prompt: "Hi", model: 'secret"; bad();' },
  ])
    assert.throws(() => validate(c));
});
test("Claude model discovery excludes upgrade-only and unavailable menu entries", () => {
  const previous = global.document;
  const item = (id, title, disabled = false) => ({
    dataset: { modelId: id },
    innerText: title,
    getClientRects: () => [{}],
    hasAttribute: () => disabled,
    getAttribute: (name) => (name === "aria-checked" ? "true" : null),
  });
  global.document = {
    querySelectorAll: (selector) => {
      assert.match(selector, /role=menuitemradio/);
      return [
        item("claude-future", "Future\nDescription"),
        item("locked", "Locked", true),
      ];
    },
  };
  try {
    assert.deepEqual(modelOptions(), [
      { id: "claude-future", title: "Future", selected: true },
    ]);
  } finally {
    global.document = previous;
  }
});
test("Claude API requests remain same-origin and preserve HTTP errors without exporting credentials", async () => {
  const previous = { location: global.location, fetch: global.fetch };
  global.location = { origin: "https://claude.ai" };
  global.fetch = async (url, options) => {
    assert.equal(url, "/api/organizations");
    assert.equal(options.credentials, "same-origin");
    return { ok: false, status: 401 };
  };
  try {
    assert.deepEqual(await apiRequest("/api/organizations"), {
      error: true,
      status: 401,
    });
    await assert.rejects(apiRequest("https://other.test/api/organizations"));
  } finally {
    Object.assign(global, previous);
  }
});

test("Claude completion requires a server stop reason and replaces UI decoration with canonical text", async () => {
  const { send } = require("../../src/providers/claude/scripts/composer.cjs");
  const events = [];
  let calls = 0,
    reads = 0,
    clock = 10000;
  const previous = Date.now;
  Date.now = () => clock;
  const p = {
    cdp: { call: async () => {} },
    evaluate: async () => {
      calls++;
      if (calls === 1) return 0;
      if (calls <= 4) return true;
      return {
        cid: id,
        count: 1,
        text: "UI text with decoration",
        working: false,
      };
    },
  };
  try {
    await send(
      { prompt: "Hello" },
      {
        p,
        base: "/api/organizations/fixture",
        emit: (x) => events.push(x),
        wait: async (fn) => fn(),
        pause: async () => {
          clock += 250;
        },
        navigate: async () => {},
        select: async () => {},
        effortMenu: async () => [],
        dismiss: async () => {},
        request: async () => {
          reads++;
          assert.equal(
            events.some((x) => x.type === "done"),
            false,
          );
          return {
            chat_messages: [
              {
                sender: "assistant",
                stop_reason: reads === 1 ? null : "end_turn",
                content: [
                  { type: "thinking", thinking: "Published summary" },
                  { type: "text", text: "Canonical answer" },
                ],
              },
            ],
          };
        },
      },
    );
    assert.equal(reads, 2);
    assert.equal(events.at(-1).type, "done");
    assert.deepEqual(events.at(-2), {
      type: "replace",
      text: "Canonical answer",
      channel: "final",
    });
    assert.deepEqual(events.at(-3), {
      type: "replace",
      text: "Published summary",
      channel: "analysis",
    });
  } finally {
    Date.now = previous;
  }
});

test("closed or fading effort menus cannot leak choices from a previous model", () => {
  const {
    effortOptions,
  } = require("../../src/providers/claude/scripts/catalog.cjs");
  const previous = global.document;
  const entry = (id, visible) => ({
    dataset: { effortId: id },
    innerText: id,
    getClientRects: () => [{}],
    checkVisibility: () => visible,
    getAttribute: () => null,
    hasAttribute: () => false,
  });
  global.document = {
    querySelectorAll: () => [entry("medium", true), entry("max", false)],
  };
  try {
    assert.deepEqual(
      effortOptions().map((x) => x.id),
      ["medium"],
    );
  } finally {
    global.document = previous;
  }
});

test("navigation waits for the new document loader before reading an old composer", async () => {
  const {
    catalogControls,
  } = require("../../src/providers/claude/scripts/catalog.cjs");
  let frames = 0,
    evaluations = 0;
  const p = {
    cdp: {
      call: async (method) => {
        if (method === "Page.navigate") return { loaderId: "new-document" };
        frames++;
        return {
          frameTree: {
            frame: { loaderId: frames === 1 ? "old-document" : "new-document" },
          },
        };
      },
    },
    evaluate: async () => {
      evaluations++;
      return true;
    },
  };
  const wait = async (fn) => {
    for (let i = 0; i < 3; i++) if (await fn()) return;
    throw Error("not ready");
  };
  await catalogControls(p, wait, async () => {}).navigate("/new");
  assert.equal(frames, 2);
  assert.equal(evaluations, 1);
});

test("model selection waits for the closing menu to disappear before the next operation", async () => {
  const {
    catalogControls,
    modelOptions,
  } = require("../../src/providers/claude/scripts/catalog.cjs");
  const options = [{ id: "claude-future", title: "Future" }];
  let closing = false,
    checks = 0;
  const p = {
    cdp: { call: async () => {} },
    evaluate: async (fn, arg) => {
      if (fn === modelOptions) {
        if (!closing) return options;
        checks++;
        return checks === 1 ? options : [];
      }
      if (arg === "claude-future") {
        closing = true;
        return;
      }
      return true;
    },
  };
  const wait = async (fn) => {
    for (let i = 0; i < 4; i++) if (await fn()) return;
    throw Error("not ready");
  };
  await catalogControls(p, wait, async () => {}).select("claude-future");
  assert.equal(checks, 2);
});

test("legacy thinking switches retain binary choices", () => {
  const {
    effortOptions,
  } = require("../../src/providers/claude/scripts/catalog.cjs");
  const previous = global.document,
    row = { innerText: "Extended\nAlways reasons deeply" };
  const toggle = {
    disabled: false,
    getClientRects: () => [{}],
    checkVisibility: () => true,
    closest: () => row,
    getAttribute: (name) => name === "aria-checked" ? "true" : null,
    hasAttribute: () => false,
  };
  global.document = {
    querySelectorAll: (selector) =>
      selector === "[data-effort-id]" ? [] : [toggle],
  };
  try {
    assert.deepEqual(
      effortOptions().map((x) => [x.id, x.selected]),
      [
        ["thinking:on", true],
        ["thinking:off", false],
      ],
    );
    toggle.disabled = true;
    assert.equal(effortOptions().length, 1);
  } finally {
    global.document = previous;
  }
});


test("Claude respects selected workspace and never silently falls back", () => {
  const { selectOrganization } = require("../../src/providers/claude/scripts/browser.cjs");
  const first = { uuid: id, capabilities: ["chat"] };
  const second = { uuid: "aaaaaaaa-abcd-1234-abcd-123456789abc", capabilities: ["chat"] };
  assert.equal(selectOrganization([first, second], second.uuid), second);
  assert.equal(selectOrganization([first], null), first);
  assert.throws(() => selectOrganization([first, second], null), /workspace/);
  assert.throws(() => selectOrganization([first], second.uuid), /workspace/);
  assert.throws(() => selectOrganization({}, null), /contract/);
});
