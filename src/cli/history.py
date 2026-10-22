"""Background-only archive snapshots and transcript preparation for the terminal."""

import threading
from types import SimpleNamespace

from accounts import origin
from providers.history import merge_conversation

from .markdown import MessageRenderer, index_transcript, transcript_fragments, visible_chat_message
from .transcript import VirtualTranscript

_render_gate = threading.Semaphore(1)


def prepare_virtual_transcript(
    messages, width, tokens, reasoning, cancelled=None, active_index=None
):
    """Build line metadata off-thread; retain only pages requested by the viewport."""
    with _render_gate:
        if cancelled and cancelled():
            raise InterruptedError("Superseded transcript")
        renderer = MessageRenderer()
        renderer.configure(tokens)
        renderer.cache_byte_limit = 256_000
        renderer.cache_limit = 512
        transcript = VirtualTranscript(
            messages,
            width,
            reasoning,
            renderer=renderer,
            cancelled=cancelled,
            active_index=active_index,
        )
        return transcript


def prepare_transcript(messages, width, tokens, reasoning, cancelled=None):
    with _render_gate:
        if cancelled and cancelled():
            raise InterruptedError("Superseded transcript")
        renderer = MessageRenderer()
        renderer.configure(tokens)
        text, ranges = renderer.transcript(messages, width, reasoning, cancelled)
        ansi = transcript_fragments(text)
        return text, ranges, ansi, index_transcript(ansi, ranges)


def prepare_archive_snapshot(
    archive,
    accounts,
    tabs,
    known_chats,
    known_projects,
    width,
    tokens,
    reasoning,
    default_account,
    revision,
    active_tab=None,
):
    values, deleted, prepared = {}, set(), []
    states = archive.states()
    state_map = {(x["account"], x["kind"], x["remote"]): x for x in states}
    for account in accounts:
        key = account["key"]
        for kind in ("chats", "containers"):
            values[key, kind, ""] = archive.get(key, kind, "")
    identities = [(x.get("id"), "conversation") for x in known_chats] + [
        (x, "container") for x in known_projects
    ]
    for (key, kind, _), page in list(values.items()):
        if page:
            identities.extend(
                (x.get("id"), "container" if kind == "containers" else "conversation")
                for x in page.get("items", [])
            )
    for tab, record, local in tabs:
        cid = record.get("conversation_id")
        if not cid:
            continue
        key, remote = origin(cid, record.get("account_key", default_account))
        identities.append((cid, "conversation"))
        state = state_map.get((key, "conversation", remote))
        if (
            not state
            or state["state"] != "ready"
            or state.get("version") == record.get("archive_version")
        ):
            continue
        value = archive.get(key, "conversation", remote)
        values[key, "conversation", remote] = value
        if value:
            messages = [
                m for x in value.get("messages", []) if (m := visible_chat_message(x)) is not None
            ]
            messages = merge_conversation({"messages": messages}, {"messages": local})["messages"]
            # Transcript views are prepared lazily for the active viewport.
            # Building a complete ANSI copy for every open tab retains large chats.
    for cid, kind in identities:
        if cid:
            key, remote = origin(cid, default_account)
            if archive.deleted(key, kind, remote):
                deleted.add((key, kind, remote))
    return SimpleNamespace(
        revision=revision,
        tab_conversations={id(tab): record.get("conversation_id") for tab, record, _ in tabs},
        get=lambda *key: values.get(key),
        deleted=lambda *key: key in deleted,
        states=lambda: states,
    ), prepared
