"use strict";
async function send(
  c,
  {
    p,
    request,
    base,
    emit,
    wait,
    pause,
    navigate,
    select,
    effortMenu,
    dismiss,
  },
) {
  await navigate(
    c.conversation_id
      ? "/chat/" + c.conversation_id
      : c.project_id
        ? "/project/" + c.project_id
        : "/new",
  );
  if (c.model) await select(c.model);
  if (c.effort) {
    const efforts = await effortMenu();
    if (!efforts.some((x) => x.id === c.effort))
      throw Error("Selected Claude effort unavailable; synchronize models");
    if (c.effort.startsWith("thinking:")) {
      await p.evaluate((enabled) => {
        const toggle = Array.from(
          document.querySelectorAll("[role=switch]"),
        ).find(
          (e) =>
            e.getClientRects().length &&
            e.checkVisibility?.({
              checkOpacity: true,
              checkVisibilityCSS: true,
            }) !== false &&
            e.closest("[role=menuitemcheckbox],[role=menuitemradio]"),
        );
        if (!toggle) throw Error("Claude thinking control changed");
        if ((toggle.getAttribute("aria-checked") === "true") !== enabled) {
          if (toggle.disabled) throw Error("Claude thinking mode is fixed");
          toggle.click();
        }
      }, c.effort === "thinking:on");
    } else {
      await p.evaluate(
        (id) =>
          Array.from(document.querySelectorAll("[data-effort-id]"))
            .find((e) => e.dataset.effortId === id)
            .click(),
        c.effort,
      );
    }
  }
  await dismiss();
  await dismiss();
  const before = await p.evaluate(
    () => document.querySelectorAll("[data-testid=assistant-message]").length,
  );
  await p.evaluate(() => {
    const e = document.querySelector("[contenteditable=true]");
    if (!e) throw Error("Claude editor unavailable");
    e.focus();
  });
  await p.cdp.call("Input.insertText", { text: c.prompt });
  await wait(
    () =>
      p.evaluate(() =>
        Array.from(document.querySelectorAll("button")).some(
          (e) =>
            !e.disabled &&
            (/^(Send message|Mesaj gönder|Gönder)$/i.test(
              e.getAttribute("aria-label") || "",
            ) ||
              ["chat-input-send", "send-button"].includes(e.dataset.testid)),
        ),
      ),
    "send button",
  );
  await p.evaluate(() =>
    Array.from(document.querySelectorAll("button"))
      .find(
        (e) =>
          !e.disabled &&
          (/^(Send message|Mesaj gönder|Gönder)$/i.test(
            e.getAttribute("aria-label") || "",
          ) ||
            ["chat-input-send", "send-button"].includes(e.dataset.testid)),
      )
      .click(),
  );

  let cid = c.conversation_id,
    last = "",
    lastThought = "",
    stable = 0,
    lastChange = Date.now(),
    lastConfirmation = 0,
    bound = false;
  const end = Date.now() + 600000;
  while (Date.now() < end) {
    const state = await p.evaluate(() => ({
      cid: location.pathname.match(/^\/chat\/([a-f0-9-]+)$/)?.[1],
      count: document.querySelectorAll("[data-testid=assistant-message]")
        .length,
      text: renderMarkdown(
        Array.from(
          document.querySelectorAll("[data-testid=assistant-message]"),
        ).at(-1),
      ),
      thoughts: renderMarkdown(
        Array.from(
          document.querySelectorAll(
            '[data-testid="thinking-content"], [data-testid="thinking-block"]',
          ),
        ).at(-1),
      ),
      working: !!document.querySelector(
        '[data-testid=stop-button],button[aria-label="Stop response"],button[aria-label="Yanıtı durdur"],button[aria-label="Durdur"],button[aria-label="Stop"]',
      ),
    }));
    if (
      state.count > before &&
      state.thoughts &&
      state.thoughts !== lastThought
    ) {
      lastThought = state.thoughts;
      lastChange = Date.now();
      emit({ type: "replace", text: lastThought, channel: "analysis" });
    }
    if (state.cid && !bound) {
      cid = state.cid;
      bound = true;
      emit({ type: "bound", id: cid });
    }
    if (state.count > before && state.text && state.text !== last) {
      last = state.text;
      lastChange = Date.now();
      stable = 0;
      emit({ type: "replace", text: last, channel: "final" });
    } else stable++;
    if (
      bound &&
      last &&
      !state.working &&
      stable >= 6 &&
      Date.now() - lastConfirmation >= 1500
    ) {
      lastConfirmation = Date.now();
      const chat = await request(
        base +
          "/chat_conversations/" +
          cid +
          "?tree=True&rendering_mode=messages",
      );
      if (c.project_id && chat.project_uuid !== c.project_id)
        throw Error(
          "Claude response received but project membership did not match",
        );
      const lastMessage = chat.chat_messages
        ?.filter((x) => x.sender === "assistant")
        .at(-1);
      if (!lastMessage?.stop_reason) {
        if (Date.now() - lastChange > 120000)
          throw Error("Claude reply timed out; prompt was not resubmitted");
        await pause(250);
        continue;
      }
      const thought = lastMessage.content
        ?.filter((x) => x.type === "thinking")
        .map((x) => x.thinking || x.text || "")
        .join("\n");
      const final = lastMessage.content
        ?.filter((x) => x.type === "text")
        .map((x) => x.text || "")
        .join("\n");
      if (thought)
        emit({ type: "replace", text: thought, channel: "analysis" });
      if (final) emit({ type: "replace", text: final, channel: "final" });
      emit({ type: "done" });
      return;
    }
    if (Date.now() - lastChange > 120000)
      throw Error("Claude reply timed out; prompt was not resubmitted");
    if (stable % 20 === 0)
      emit({ type: "status", message: "Claude yanıtı bekleniyor…" });
    await pause(250);
  }
  throw Error("Claude response exceeded the time limit");
}
module.exports = { send };
