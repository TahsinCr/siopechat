"""Background-only archive snapshots and transcript preparation for the terminal."""

import threading
from types import SimpleNamespace

from accounts import origin

from .markdown import MessageRenderer, index_transcript, transcript_fragments
from .transcript import VirtualTranscript

_render_gate = threading.Semaphore(1)


def prepare_virtual_transcript(
    messages, width, tokens, reasoning, cancelled=None, active_index=None, renderer=None
):
    """Build line metadata off-thread; retain only pages requested by the viewport."""
    with _render_gate:
        if cancelled and cancelled():
            raise InterruptedError("Superseded transcript")
        if renderer is None:
            renderer = MessageRenderer()
            renderer.configure(tokens)
        renderer.cache_byte_limit = 1_000_000
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


def prepare_transcript(
    messages,
    width,
    tokens,
    reasoning,
    cancelled=None,
    *,
    renderer=None,
    active_index=None,
    index_rows=True,
):
    with _render_gate:
        if cancelled and cancelled():
            raise InterruptedError("Superseded transcript")
        if renderer is None:
            renderer = MessageRenderer()
            renderer.configure(tokens)
        text, ranges = renderer.transcript(messages, width, reasoning, cancelled, active_index)
        ansi = transcript_fragments(text)
        return text, ranges, ansi, index_transcript(ansi, ranges) if index_rows else ()


def prepare_archive_snapshot(
    archive, accounts, tabs, known_chats, known_projects, default_account, revision
):
    values, deleted = {}, set()
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
    for tab, record in tabs:
        cid = record.get("conversation_id")
        if not cid:
            continue
        key, remote = origin(cid, record.get("account_key", default_account))
        identities.append((cid, "conversation"))
        state = state_map.get((key, "conversation", remote))
        if (
            record.get("busy")
            or not state
            or state["state"] != "ready"
            or state.get("version") == record.get("archive_version")
        ):
            continue
        window = record.get("history_window") or {}
        value = archive.conversation_window(
            key, remote, start=None if record.get("follow_output", True) else window.get("start")
        )
        values[key, "conversation", remote] = value
    for cid, kind in identities:
        if cid:
            key, remote = origin(cid, default_account)
            if archive.deleted(key, kind, remote):
                deleted.add((key, kind, remote))
    return SimpleNamespace(
        revision=revision,
        tab_windows={id(tab): record.get("history_window") for tab, record in tabs},
        get=lambda *key: values.get(key),
        deleted=lambda *key: key in deleted,
        states=lambda: states,
    )
