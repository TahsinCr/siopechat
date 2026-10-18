"use strict";
const fs = require("node:fs");
const { openPage } = require("../../scripts/page.cjs");
const { modelOptions, catalogControls } = require("./catalog.cjs");
const { send } = require("./composer.cjs");

const emit = (x) => process.stdout.write(JSON.stringify(x) + "\n");
const pause = (ms) => new Promise((r) => setTimeout(r, ms));
const uuid = (value) =>
  typeof value === "string" &&
  /^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/i.test(value);
function selectOrganization(organizations, preferred) {
  if (!Array.isArray(organizations))
    throw Error("Claude organization contract changed");
  const usable = organizations.filter(
    (x) =>
      uuid(x.uuid) &&
      Array.isArray(x.capabilities) &&
      x.capabilities.includes("chat"),
  );
  if (preferred) {
    const selected = usable.find((x) => x.uuid === preferred);
    if (!selected)
      throw Error("Selected Claude workspace is unavailable; select a workspace in Claude");
    return selected;
  }
  if (usable.length > 1)
    throw Error("Select a workspace in Claude before synchronizing");
  return usable[0];
}
function validate(c) {
  if (
    ![
      "status",
      "models",
      "chats",
      "history",
      "projects",
      "project",
      "project_chats",
      "create_project",
      "delete_project",
      "delete_chat",
      "rename_chat",
      "move_chat",
      "send",
      "close",
    ].includes(c.mode)
  )
    throw Error("Unknown Claude operation");
  for (const key of ["conversation_id", "project_id"])
    if (c[key] != null && !uuid(c[key])) throw Error("Invalid Claude identity");
  if (c.mode === "send" && (typeof c.prompt !== "string" || !c.prompt.trim()))
    throw Error("Empty prompt");
  if (
    ["history", "delete_chat", "rename_chat", "move_chat"].includes(c.mode) &&
    !uuid(c.conversation_id)
  )
    throw Error("Conversation identity required");
  if (
    ["project", "project_chats", "delete_project"].includes(c.mode) &&
    !uuid(c.project_id)
  )
    throw Error("Project identity required");
  if (c.model && !/^claude-[a-z0-9-]+$/.test(c.model))
    throw Error("Invalid Claude model");
  if (c.cursor && !/^\d+$/.test(c.cursor)) throw Error("Invalid Claude page");
}
async function apiRequest(path, method = "GET", body = null) {
  if (
    location.origin !== "https://claude.ai" ||
    !path.startsWith("/api/") ||
    path.includes("..")
  )
    throw Error("Invalid Claude API path");
  const r = await fetch(path, {
    method,
    credentials: "same-origin",
    headers: { "content-type": "application/json" },
    body: body === null ? undefined : JSON.stringify(body),
    signal: AbortSignal.timeout(30000),
  });
  if (!r.ok) return { error: true, status: r.status };
  return { data: r.status === 204 ? null : await r.json() };
}
async function run(c) {
  validate(c);
  const p = await openPage(c, "https://claude.ai", "/new", (id) =>
    emit({ type: "tab", id }),
  );
  if (!p) return;

  const cancel = () => {
    (c.mode === "send"
      ? p.evaluate(() =>
          document
            .querySelector(
              '[data-testid=stop-button],button[aria-label="Stop response"],button[aria-label="Yanıtı durdur"],button[aria-label="Durdur"],button[aria-label="Stop"]',
            )
            ?.click(),
        )
      : Promise.resolve()
    )
      .catch(() => {})
      .finally(() => p.close())
      .finally(() => process.exit(0));
  };
  process.once("SIGTERM", cancel);
  process.once("SIGINT", cancel);
  const wait = async (fn, label, seconds = 30) => {
    const end = Date.now() + seconds * 1000;
    while (Date.now() < end) {
      const v = await fn();
      if (v) return v;
      await pause(150);
    }
    throw Error("Claude " + label + " did not become ready");
  };
  const request = async (path, method, body) => {
    const r = await p.evaluate(apiRequest, path, method, body);
    if (r.error) {
      const e = Error("Claude operation failed (HTTP " + r.status + ")");
      e.status = r.status;
      throw e;
    }
    return r.data;
  };
  const catalog = catalogControls(p, wait, pause);

  try {
    await wait(
      () =>
        p.evaluate(
          () =>
            location.origin === "https://claude.ai" &&
            document.readyState !== "loading",
        ),
      "page",
    );
    const organizations = await request("/api/organizations");
    const preferred = await p.evaluate(() => {
      const match = document.cookie.match(/(?:^|;\s*)lastActiveOrg=([^;]*)/);
      return match ? decodeURIComponent(match[1]) : null;
    });
    const org = selectOrganization(organizations, preferred);
    if (!org) {
      const e = Error("Claude chat account is not signed in");
      e.status = 401;
      throw e;
    }
    const base = "/api/organizations/" + org.uuid;
    if (c.mode === "status") {
      emit({ type: "result", data: { authenticated: true } });
      return;
    }
    if (c.mode === "models") {
      emit({ type: "result", data: await catalog.discover() });
      return;
    }
    if (c.mode === "chats") {
      emit({
        type: "result",
        data: await request(
          base +
            "/chat_conversations?limit=" +
            Math.min(100, Number(c.limit) || 50) +
            "&offset=" +
            (Number(c.cursor) || 0),
        ),
      });
      return;
    }
    if (c.mode === "history") {
      emit({
        type: "result",
        data: await request(
          base +
            "/chat_conversations/" +
            c.conversation_id +
            "?tree=True&rendering_mode=messages&render_all_tools=true",
        ),
      });
      return;
    }
    if (c.mode === "projects") {
      emit({ type: "result", data: await request(base + "/projects") });
      return;
    }
    if (c.mode === "project") {
      const path = base + "/projects/" + c.project_id;
      const [detail, docs, files] = await Promise.all([
        request(path),
        request(path + "/docs"),
        request(path + "/files"),
      ]);
      if (!Array.isArray(docs) || !Array.isArray(files))
        throw Error("Claude project resource contract changed");
      emit({ type: "result", data: { ...detail, docs, files } });
      return;
    }
    if (c.mode === "project_chats") {
      emit({
        type: "result",
        data: await request(
          base + "/projects/" + c.project_id + "/conversations",
        ),
      });
      return;
    }
    if (c.mode === "create_project") {
      emit({
        type: "result",
        data: await request(base + "/projects", "POST", {
          name: c.name,
          description: "",
          prompt_template: c.instructions || "",
          is_private: true,
        }),
      });
      return;
    }
    if (c.mode === "delete_project") {
      await request(base + "/projects/" + c.project_id, "DELETE");
      emit({ type: "result", data: null });
      return;
    }
    if (c.mode === "rename_chat") {
      if (typeof c.title !== "string" || !c.title.trim())
        throw Error("Chat title required");
      await request(base + "/chat_conversations/" + c.conversation_id, "PUT", {
        name: c.title,
      });
      const chat = await request(
        base + "/chat_conversations/" + c.conversation_id,
      );
      if (chat.name !== c.title)
        throw Error("Claude chat rename could not be verified");
      emit({ type: "result", data: null });
      return;
    }
    if (c.mode === "delete_chat") {
      await request(
        base + "/chat_conversations/" + c.conversation_id,
        "DELETE",
      );
      emit({ type: "result", data: null });
      return;
    }
    if (c.mode === "move_chat") {
      await request(base + "/chat_conversations/move_many", "POST", {
        conversation_uuids: [c.conversation_id],
        project_uuid: c.project_id || null,
      });
      const chat = await request(
        base + "/chat_conversations/" + c.conversation_id,
      );
      if ((chat.project_uuid || null) !== (c.project_id || null))
        throw Error("Claude project membership could not be verified");
      emit({ type: "result", data: null });
      return;
    }
    if (c.mode !== "send") throw Error("Unsupported Claude operation");
    await send(c, { p, request, base, emit, wait, pause, ...catalog });
  } finally {
    process.removeListener("SIGTERM", cancel);
    process.removeListener("SIGINT", cancel);
    await p.close();
  }
}
module.exports = { validate, modelOptions, apiRequest, selectOrganization };
if (require.main === module) {
  const raw = fs.readFileSync(0, "utf8");
  if (raw.length > 2 * 1024 * 1024) throw Error("Input too large");
  run(JSON.parse(raw)).catch((error) => {
    emit({ type: "error", message: error.message, status: error.status });
    process.exitCode = 1;
  });
}
